"""The ``smtp`` backend against the in-process stub: real TLS, no sockets but a socketpair."""

import logging
import re
import socket
import ssl
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr
from pytest_socket import SocketBlockedError

from earmark.config import ConfigError, Settings
from earmark.errors import EarmarkError, MaybeDelivered, PermanentError, TransientError
from earmark.mail import smtp
from earmark.mail.smtp import SmtpMailer, SmtpOptions, build_smtp_mailer, enhanced_status
from earmark.pipeline.stages import OutgoingMail
from tests.factories import TO
from tests.smtp_stub import (
    ECHOED_ADDRESS,
    REPLY_SENTINEL,
    RecordingContextFactory,
    SmtpStub,
    stub_factory,
    trusted_test_context,
)

make_stub = pytest.fixture(stub_factory)
StubMaker = Callable[..., SmtpStub]

PASSWORD = "abcd efgh ijkl mnop"
SENDER = "earmark@example.org"
JOB_ID = "0123456789abcdef" * 2
DATA = (
    b"From: earmark@example.org\r\nTo: you@example.org\r\nSubject: hi\r\n\r\n"
    b"first line\r\n.leading dot\r\n..two dots\r\n.\r\nlast line\r\n"
)
SOURCE = Path(__file__).resolve().parents[1] / "src" / "earmark"


def options(
    security: str = "starttls", *, timeout_s: float = 5.0, host: str = "smtp.test"
) -> SmtpOptions:
    return SmtpOptions(
        host=host,
        port=465 if security == "tls" else 587,
        security=security,  # type: ignore[arg-type]  # a Literal in production
        username="mailer",
        password=SecretStr(PASSWORD),
        sender=SENDER,
        timeout_s=timeout_s,
    )


def make_mail(data: bytes = DATA) -> OutgoingMail:
    return OutgoingMail(
        job_id=JOB_ID,
        message_id=f"<earmark.{JOB_ID}@example.org>",
        sender=SENDER,
        recipient=TO,
        data=data,
        note_filename="n.md",
        instructions_filename="n.instructions.md",
    )


def mailer(
    stub: SmtpStub,
    security: str = "starttls",
    *,
    timeout_s: float = 5.0,
    contexts: RecordingContextFactory | None = None,
    host: str = "smtp.test",
) -> SmtpMailer:
    return SmtpMailer(
        options(security, timeout_s=timeout_s, host=host),
        socket_factory=stub.socket_factory,
        context_factory=contexts or trusted_test_context,
    )


def failure(m: SmtpMailer, mail: OutgoingMail | None = None) -> EarmarkError:
    with pytest.raises(EarmarkError) as info:
        m.send(mail or make_mail())
    return info.value


def assert_no_leaks(exc: EarmarkError) -> None:
    for secret in (REPLY_SENTINEL, ECHOED_ADDRESS, TO, PASSWORD, "mailer", "pka@"):
        assert secret not in exc.message
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__  # smtplib's text is not chained


MODES = ["starttls", "tls"]


def stub_for(make_stub: StubMaker, security: str, **knobs: object) -> SmtpStub:
    return make_stub(implicit_tls=security == "tls", **knobs)


# -- the stub itself ---------------------------------------------------------------------


def test_stub_runs_with_sockets_blocked(make_stub: StubMaker) -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stub = make_stub()
    mailer(stub).send(make_mail())
    assert stub.messages == [DATA]


# -- the happy path ----------------------------------------------------------------------


@pytest.mark.parametrize("security", MODES)
def test_send_delivers_the_message(make_stub: StubMaker, security: str) -> None:
    stub = stub_for(make_stub, security)
    mailer(stub, security).send(make_mail())
    stub.join()
    assert stub.messages == [DATA]  # dot-stuffing was undone by the server
    assert stub.mail_from == [SENDER]
    assert stub.rcpt_to == [TO]
    assert stub.auth == [("mailer", PASSWORD, True)]
    assert stub.connections == 1
    assert stub.verbs[-1] == "QUIT"


def test_data_without_final_crlf_is_terminated(make_stub: StubMaker) -> None:
    stub = make_stub()
    mailer(stub).send(make_mail(b"line one\r\nlast line"))
    assert stub.messages == [b"line one\r\nlast line\r\n"]


def test_envelope_is_the_one_validated_recipient(make_stub: StubMaker) -> None:
    stub = make_stub()
    data = b"To: other@example.net\r\nBcc: bcc@example.net\r\nCc: cc@example.net\r\n\r\nx\r\n"
    mailer(stub).send(make_mail(data))
    assert stub.rcpt_to == [TO]
    assert stub.mail_from == [SENDER]
    assert stub.verbs.count("RCPT") == 1


def test_auth_only_after_tls(make_stub: StubMaker) -> None:
    stub = make_stub()
    mailer(stub).send(make_mail())
    plain = [verb for tls, verb in stub.commands if not tls]
    assert plain == ["EHLO", "STARTTLS"]
    assert stub.auth
    assert all(over_tls for _, _, over_tls in stub.auth)
    first_auth = stub.verbs.index("AUTH")
    assert all(tls for tls, _ in stub.commands[first_auth:])


def test_login_mechanism_login(make_stub: StubMaker) -> None:
    stub = make_stub(auth_mechanisms="LOGIN")
    mailer(stub).send(make_mail())
    assert stub.auth == [("mailer", PASSWORD, True)]


def test_ehlo_uses_from_domain(make_stub: StubMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise AssertionError("socket.getfqdn must not be called")

    monkeypatch.setattr(socket, "getfqdn", boom)
    starttls = make_stub()
    mailer(starttls).send(make_mail())
    assert starttls.ehlo_names == ["example.org", "example.org"]
    implicit = make_stub(implicit_tls=True)
    mailer(implicit, "tls").send(make_mail())
    assert implicit.ehlo_names == ["example.org"]


def test_success_logs_host_and_job_only(
    make_stub: StubMaker, caplog: pytest.LogCaptureFixture
) -> None:
    stub = make_stub()
    with caplog.at_level(logging.DEBUG):
        mailer(stub).send(make_mail())
    text = caplog.text
    assert f"smtp: message for job {JOB_ID} accepted by smtp.test" in text
    for secret in (TO, SENDER, PASSWORD, REPLY_SENTINEL, ECHOED_ADDRESS):
        assert secret not in text


# -- TLS ---------------------------------------------------------------------------------


@pytest.mark.parametrize("security", MODES)
def test_tls_context_verifies(make_stub: StubMaker, security: str) -> None:
    contexts = RecordingContextFactory()
    stub = stub_for(make_stub, security)
    mailer(stub, security, contexts=contexts).send(make_mail())
    [context] = contexts.contexts
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_default_context_factory_is_create_default_context() -> None:
    assert smtp.DEFAULT_CONTEXT_FACTORY is ssl.create_default_context
    context = smtp.DEFAULT_CONTEXT_FACTORY()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize("security", MODES)
def test_untrusted_certificate_fails_permanently(make_stub: StubMaker, security: str) -> None:
    stub = stub_for(make_stub, security)
    m = SmtpMailer(
        options(security),
        socket_factory=stub.socket_factory,
        context_factory=smtp.DEFAULT_CONTEXT_FACTORY,  # the system trust store: not the test CA
    )
    exc = failure(m)
    assert isinstance(exc, PermanentError)
    assert "EARMARK_SMTP_HOST" in exc.message
    assert "did not verify" in exc.message
    stub.join()
    assert stub.auth == []
    assert stub.messages == []
    assert_no_leaks(exc)


@pytest.mark.parametrize("security", MODES)
def test_hostname_mismatch_fails_permanently(make_stub: StubMaker, security: str) -> None:
    stub = stub_for(make_stub, security)
    exc = failure(mailer(stub, security, host="other.test"))
    assert isinstance(exc, PermanentError)
    assert "other.test" in exc.message
    assert "EARMARK_SMTP_HOST" in exc.message
    stub.join()
    assert stub.auth == []


def test_no_starttls_fails_permanently_without_auth(make_stub: StubMaker) -> None:
    stub = make_stub(offer_starttls=False)
    exc = failure(mailer(stub))
    assert isinstance(exc, PermanentError)
    assert "does not offer STARTTLS" in exc.message
    assert "never sends in plaintext" in exc.message
    stub.join()
    assert set(stub.verbs) <= {"EHLO", "QUIT"}
    assert all(not tls for tls, _ in stub.commands)
    assert stub.auth == []
    assert stub.messages == []


# -- failure classes ---------------------------------------------------------------------

REPLIES = {
    "mail": "mail_reply",
    "rcpt": "rcpt_reply",
    "data_command": "data_reply",
    "final": "final_reply",
}
PHASE_WORDS = {
    "mail": "the sender",
    "rcpt": "the recipient",
    "data_command": "DATA",
    "final": "the message",
}


@pytest.mark.parametrize("security", MODES)
@pytest.mark.parametrize("where", REPLIES)
def test_reply_4xx_is_transient(make_stub: StubMaker, where: str, security: str) -> None:
    reply = f"451 4.3.0 try again later {REPLY_SENTINEL} {ECHOED_ADDRESS}"
    stub = stub_for(make_stub, security, **{REPLIES[where]: reply})
    exc = failure(mailer(stub, security))
    assert type(exc) is TransientError
    assert exc.message == (
        f"the SMTP server at smtp.test refused {PHASE_WORDS[where]} (reply 451, 4.3.0)"
    )
    assert_no_leaks(exc)


@pytest.mark.parametrize("where", REPLIES)
def test_reply_5xx_is_permanent(make_stub: StubMaker, where: str) -> None:
    reply = f"550 5.1.1 no such user {REPLY_SENTINEL} {ECHOED_ADDRESS}"
    stub = make_stub(**{REPLIES[where]: reply})
    exc = failure(mailer(stub))
    assert type(exc) is PermanentError
    assert exc.message == (
        f"the SMTP server at smtp.test refused {PHASE_WORDS[where]} (reply 550, 5.1.1)"
    )
    assert_no_leaks(exc)


def test_reply_without_enhanced_code_has_only_the_reply_code(make_stub: StubMaker) -> None:
    stub = make_stub(rcpt_reply=f"554 mailbox unavailable {ECHOED_ADDRESS}")
    exc = failure(mailer(stub))
    assert exc.message == "the SMTP server at smtp.test refused the recipient (reply 554)"


def test_a_refused_ehlo_is_a_reply_error(make_stub: StubMaker) -> None:
    exc = failure(mailer(make_stub(ehlo_reply_code=554)))
    assert type(exc) is PermanentError
    assert exc.message == "the SMTP server at smtp.test refused EHLO (reply 554, 5.5.1)"
    assert_no_leaks(exc)


@pytest.mark.parametrize("security", MODES)
def test_a_refused_greeting_is_a_reply_error(make_stub: StubMaker, security: str) -> None:
    stub = stub_for(make_stub, security, greeting=f"554 5.3.2 go away {ECHOED_ADDRESS}")
    exc = failure(mailer(stub, security))
    assert type(exc) is PermanentError
    assert exc.message == "the SMTP server at smtp.test refused the connection (reply 554, 5.3.2)"
    assert_no_leaks(exc)


def test_an_unusable_final_reply_is_maybe_delivered(make_stub: StubMaker) -> None:
    stub = make_stub(final_reply="what is this")
    exc = failure(mailer(stub))
    assert type(exc) is MaybeDelivered
    assert "did not confirm the message" in exc.message


@pytest.mark.parametrize("security", MODES)
@pytest.mark.parametrize("where", ["greeting", "mail", "data_command", "mid_payload"])
def test_connection_lost_before_payload_is_transient(
    make_stub: StubMaker, where: str, security: str
) -> None:
    stub = stub_for(make_stub, security, disconnect_at=where)
    # A message of 4 MiB or more, so the client is still writing when the stub hangs up; a
    # small payload would fit in the socket buffer and correctly count as fully sent.
    big = b"".join(b"x" * 70 + b"\r\n" for _ in range(60_000))
    assert len(big) >= 4 * 1024 * 1024
    exc = failure(mailer(stub, security), make_mail(big))
    assert type(exc) is TransientError
    assert not isinstance(exc, MaybeDelivered)
    assert exc.message.startswith("lost the connection to the SMTP server at smtp.test before")
    assert "before the message was sent" in exc.message
    assert_no_leaks(exc)
    stub.join()
    assert stub.messages == []


@pytest.mark.parametrize("security", MODES)
def test_disconnect_after_payload_is_maybe_delivered(make_stub: StubMaker, security: str) -> None:
    stub = stub_for(make_stub, security, disconnect_at="after_payload")
    exc = failure(mailer(stub, security))
    assert type(exc) is MaybeDelivered
    assert isinstance(exc, TransientError)
    assert "will be sent again" in exc.message
    assert "may get it twice" in exc.message
    assert_no_leaks(exc)
    stub.join()
    assert stub.messages == [DATA]  # the server did receive it


def test_stall_after_payload_is_maybe_delivered_within_deadline(make_stub: StubMaker) -> None:
    stub = make_stub(stall_after_payload=True)
    start = time.monotonic()
    exc = failure(mailer(stub, timeout_s=0.6))
    assert type(exc) is MaybeDelivered
    assert time.monotonic() - start < 3.0


def test_session_deadline_bounds_a_dripping_server(make_stub: StubMaker) -> None:
    reply = "250 2.0.0 ok " + "x" * 15  # 28 characters plus CRLF: 30 bytes at 0.2 s each
    stub = make_stub(final_reply=reply, drip_s=0.2)
    start = time.monotonic()
    exc = failure(mailer(stub, timeout_s=0.5))  # every byte beats the per-operation timeout
    elapsed = time.monotonic() - start
    assert type(exc) is MaybeDelivered
    assert "EARMARK_SMTP_TIMEOUT_S (0.5 s)" in exc.message
    assert elapsed < 2.0
    assert_no_leaks(exc)


def test_auth_5xx_is_permanent_and_names_settings(make_stub: StubMaker) -> None:
    stub = make_stub(auth_reply="535 5.7.8 bad credentials for {user}: {password}")
    exc = failure(mailer(stub))
    assert type(exc) is PermanentError
    assert exc.message == (
        "the SMTP server at smtp.test rejected the login (reply 535, 5.7.8); "
        "check EARMARK_SMTP_USERNAME and EARMARK_SMTP_PASSWORD"
    )
    assert_no_leaks(exc)
    stub.join()
    assert stub.messages == []
    assert "MAIL" not in stub.verbs


def test_auth_4xx_is_transient(make_stub: StubMaker) -> None:
    stub = make_stub(auth_reply=f"454 4.7.0 try later {ECHOED_ADDRESS}")
    exc = failure(mailer(stub))
    assert type(exc) is TransientError
    assert exc.message == "the SMTP server at smtp.test refused the login (reply 454, 4.7.0)"
    assert_no_leaks(exc)


def test_no_auth_offered_is_permanent(make_stub: StubMaker) -> None:
    exc = failure(mailer(make_stub(auth_mechanisms=None)))
    assert type(exc) is PermanentError
    assert "does not offer AUTH over TLS" in exc.message
    assert_no_leaks(exc)


def test_no_supported_mechanism_is_permanent(make_stub: StubMaker) -> None:
    exc = failure(mailer(make_stub(auth_mechanisms="XOAUTH2 GSSAPI")))
    assert type(exc) is PermanentError
    assert "offers no supported AUTH mechanism (PLAIN, LOGIN or CRAM-MD5)" in exc.message


def test_connect_refused_is_transient() -> None:
    def refuse(host: str, port: int, timeout: float) -> socket.socket:
        raise ConnectionRefusedError("refused to pka@example.net")

    m = SmtpMailer(options(), socket_factory=refuse, context_factory=trusted_test_context)
    exc = failure(m)
    assert type(exc) is TransientError
    assert exc.message == (
        "lost the connection to the SMTP server at smtp.test before the message was sent "
        "(ConnectionRefusedError)"
    )
    assert_no_leaks(exc)


# -- the login check (doctor --online) ---------------------------------------------------


@pytest.mark.parametrize("security", MODES)
def test_check_login_sends_nothing(make_stub: StubMaker, security: str) -> None:
    stub = stub_for(make_stub, security)
    report = mailer(stub, security).check_login()
    stub.join()
    assert report.tls_version.startswith("TLSv1.")
    assert stub.auth == [("mailer", PASSWORD, True)]
    for verb in ("MAIL", "RCPT", "DATA"):
        assert verb not in stub.verbs
    assert stub.verbs[-1] == "QUIT"
    assert stub.messages == []


def test_check_login_failures_name_the_check(make_stub: StubMaker) -> None:
    stub = make_stub(disconnect_at="greeting")
    exc = failure_of(lambda: mailer(stub).check_login())
    assert type(exc) is TransientError
    assert exc.message.endswith("during the login check (SMTPServerDisconnected)")
    rejected = make_stub(auth_reply="535 5.7.8 nope")
    exc = failure_of(lambda: mailer(rejected).check_login())
    assert type(exc) is PermanentError
    assert "EARMARK_SMTP_PASSWORD" in exc.message


def failure_of(call: Callable[[], object]) -> EarmarkError:
    with pytest.raises(EarmarkError) as info:
        call()
    return info.value


# -- options, builder, helpers -----------------------------------------------------------


def settings(**kw: object) -> Settings:
    values: dict[str, object] = {
        "smtp_host": "smtp.example.org",
        "smtp_username": "mailer",
        "smtp_password": SecretStr(PASSWORD),
        "smtp_from": SENDER,
    }
    values.update(kw)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_options_from_settings() -> None:
    opts = SmtpOptions.from_settings(settings())
    assert (opts.host, opts.port, opts.security, opts.sender) == (
        "smtp.example.org",
        587,
        "starttls",
        SENDER,
    )
    assert opts.local_hostname == "example.org"
    assert opts.timeout_s == 60.0
    tls = SmtpOptions.from_settings(settings(smtp_security="tls", smtp_timeout_s=12.5))
    assert (tls.port, tls.security, tls.timeout_s) == (465, "tls", 12.5)
    assert SmtpOptions.from_settings(settings(smtp_port=2525)).port == 2525


def test_options_report_every_missing_variable() -> None:
    with pytest.raises(ConfigError) as info:
        SmtpOptions.from_settings(Settings(_env_file=None))
    names = [line.split(":")[0] for line in info.value.errors]
    assert names == [
        "EARMARK_SMTP_HOST",
        "EARMARK_SMTP_FROM",
        "EARMARK_SMTP_USERNAME",
        "EARMARK_SMTP_PASSWORD",
    ]
    with pytest.raises(ConfigError) as partial:
        SmtpOptions.from_settings(settings(smtp_from=None, smtp_username=None))
    assert [e.split(":")[0] for e in partial.value.errors] == [
        "EARMARK_SMTP_FROM",
        "EARMARK_SMTP_USERNAME",
    ]


def test_build_makes_no_connection_and_logs_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="earmark.mail.smtp"):
        built = build_smtp_mailer(settings())  # sockets are blocked: any connect would raise
    assert built.sender == SENDER
    built.close()
    assert [r.getMessage() for r in caplog.records] == [
        "mail via smtp to smtp.example.org:587 (starttls), from domain example.org"
    ]
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"2.0.0 queued", "2.0.0"),
        (b"  5.1.1 no such user", "5.1.1"),
        (b"4.7.12 later", "4.7.12"),
        (b"5.1.1", "5.1.1"),
        (b"3.0.0 odd", None),
        (b"queued as 5.1.1", None),
        (b"5.1.1234 x", None),
        (b"5.1 x", None),
        (b"", None),
    ],
)
def test_enhanced_status(reply: bytes, expected: str | None) -> None:
    assert enhanced_status(reply) == expected


def test_source_never_sets_debuglevel_or_makes_msgid() -> None:
    pattern = re.compile(r"make_msgid|set_debuglevel|getfqdn|_create_stdlib_context|sendmail\(")
    files = sorted((SOURCE / "mail").glob("*.py"))
    assert {f.name for f in files} >= {"smtp.py", "message.py", "note.py"}
    for path in files:
        assert not pattern.search(path.read_text()), path.name


def test_live_test_is_skipped_without_its_variables() -> None:
    from tests.test_smtp_live import REQUIRED, missing_variables

    assert missing_variables({}) == list(REQUIRED)
    assert missing_variables(dict.fromkeys(REQUIRED, "x")) == []
    assert missing_variables({**dict.fromkeys(REQUIRED, "x"), "SMTP_TEST_TO": ""}) == [
        "SMTP_TEST_TO"
    ]


def test_tls_fixtures_are_test_only() -> None:
    tls = Path(__file__).parent / "fixtures" / "tls"
    assert sorted(p.name for p in tls.iterdir()) == [
        "README.md",
        "ca.pem",
        "make-certs.sh",
        "server.key",
        "server.pem",
    ]  # in particular: no CA key, so nothing else can be signed
    script = (tls / "make-certs.sh").read_text()
    assert "TEST-ONLY" in script
    assert "DNS:smtp.test" in script
    assert "CN=smtp.test" in script
    assert "ca.key" in script.split("rm -f", 1)[1]  # the CA key is deleted after signing
    assert "test-only" in (tls / "README.md").read_text()
    # The server certificate really is for smtp.test and for nothing else.
    cert = ssl._ssl._test_decode_cert(str(tls / "server.pem"))  # type: ignore[attr-defined]
    assert cert["subject"] == ((("commonName", "smtp.test"),),)
    assert cert["subjectAltName"] == (("DNS", "smtp.test"),)
