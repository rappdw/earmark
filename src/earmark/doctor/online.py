"""The ``--online`` doctor checks: reachability only, read-only, and free.

Every HTTP client here follows the rules of ``notify.ntfy`` (no redirects, no proxy
variables, no cookies, a capped response, host-only messages). Nothing is published,
downloaded, or billed.
"""

import json
from collections.abc import Sequence
from typing import Final

import httpx2
from pydantic import SecretStr

from earmark.config import Settings
from earmark.doctor import Probes
from earmark.doctor.checks import OfflineChecks, collapse, guarded
from earmark.doctor.report import CheckResult, doctor_secrets
from earmark.endpoint import error_detail, host_label
from earmark.errors import EarmarkError
from earmark.fetch import USER_AGENT, BlockedAddressError, GuardedFetcher
from earmark.mail.smtp import DEFAULT_CONTEXT_FACTORY, SmtpMailer, SmtpOptions
from earmark.notify.ntfy import NtfyAuthError, NtfyClient, NtfyOptions
from earmark.resolve.itunes import LOOKUP_URL

MAX_BODY_BYTES: Final = 64 * 1024
MAX_LISTED_MODELS: Final = 5
MAX_MODEL_CHARS: Final = 100
OFFLINE_FAILED: Final = "offline check failed"
ALLOW_PRIVATE_HINT: Final = (
    "; if a filtering DNS resolver or the sandbox answers private addresses, "
    "see EARMARK_ALLOW_PRIVATE_FETCH"
)


def _timeout(timeout_s: float) -> httpx2.Timeout:
    return httpx2.Timeout(timeout_s, connect=min(5.0, timeout_s))


def probe_models(
    base_url: str,
    api_key: SecretStr | None,
    model: str,
    *,
    setting: str,  # "EARMARK_ASR", used in messages
    transport: httpx2.BaseTransport | None,
    timeout_s: float,
    secrets: Sequence[str],
) -> CheckResult:
    """``GET {base_url}/models``; never raises."""
    name = "asr server"
    host = host_label(base_url)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    secret_list = list(secrets)
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        secret_list.append(api_key.get_secret_value())
    status = 0
    content_type: str | None = None
    body = b""
    try:
        with (
            httpx2.Client(
                transport=transport or httpx2.HTTPTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=_timeout(timeout_s),
            ) as client,
            client.stream("GET", f"{base_url}/models", headers=headers) as response,
        ):
            status = response.status_code
            content_type = response.headers.get("Content-Type")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY_BYTES:
                    break
            body = b"".join(chunks)[:MAX_BODY_BYTES]
    except httpx2.TimeoutException:
        return CheckResult(name, "fail", f"{host} did not answer within {timeout_s:g} s")
    except (httpx2.TransportError, httpx2.DecodingError) as exc:
        return CheckResult(name, "fail", f"could not reach {host}: {type(exc).__name__}")
    except httpx2.InvalidURL:
        return CheckResult(name, "fail", f"invalid {setting}_BASE_URL")
    if 300 <= status < 400:
        return CheckResult(
            name,
            "fail",
            f"{host} answered with a redirect (HTTP {status}); redirects are not "
            f"followed, check {setting}_BASE_URL",
        )
    if status in (401, 403):
        return CheckResult(
            name, "fail", f"{host} refused the request: HTTP {status} (check {setting}_API_KEY)"
        )
    if status == 404:
        return CheckResult(
            name,
            "warn",
            f"{host} is reachable but has no /models endpoint; cannot check {setting}_MODEL",
        )
    if status != 200:
        detail = error_detail(body, content_type, secret_list)
        return CheckResult(
            name, "fail", f"{host} answered HTTP {status}" + (f": {detail}" if detail else "")
        )
    ids = _model_ids(body)
    if ids is None:
        return CheckResult(name, "warn", f"{host} answered, but not with a model list")
    if model in ids:
        return CheckResult(name, "ok", f"{host} serves model {model}")
    shown = ", ".join(collapse(i)[:MAX_MODEL_CHARS] for i in ids[:MAX_LISTED_MODELS])
    return CheckResult(name, "warn", f"{host} does not list model {model} (it lists: {shown})")


def _model_ids(body: bytes) -> list[str] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    listed = data.get("data") if isinstance(data, dict) else None
    if not isinstance(listed, list):
        return None
    ids: list[str] = []
    for entry in listed:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            return None
        ids.append(entry["id"])
    return ids


def _by_name(results: Sequence[CheckResult], name: str) -> CheckResult | None:
    return next((r for r in results if r.name == name), None)


def _needs_ok(results: Sequence[CheckResult], needs: str) -> bool:
    found = _by_name(results, needs)
    return found is not None and found.status != "fail"


def run_online(
    settings: Settings,
    probes: Probes,
    offline: Sequence[CheckResult],
    checks: OfflineChecks,
) -> list[CheckResult]:
    s = settings
    secrets = doctor_secrets(s)
    results: list[CheckResult] = []

    def skipped(name: str) -> CheckResult:
        return CheckResult(name, "skip", OFFLINE_FAILED)

    if s.resolver_backend == "apple":
        results.append(
            guarded("apple lookup", lambda: apple_lookup(s, probes))
            if _needs_ok(offline, "resolver")
            else skipped("apple lookup")
        )
    if s.asr_backend == "remote":
        if _needs_ok(offline, "asr") and s.asr_base_url is not None:
            base = s.asr_base_url
            results.append(
                guarded(
                    "asr server",
                    lambda: probe_models(
                        base,
                        s.asr_api_key,
                        s.asr_model,
                        setting="EARMARK_ASR",
                        transport=probes.http_transport,
                        timeout_s=probes.timeout_s,
                        secrets=secrets,
                    ),
                )
            )
        else:
            results.append(skipped("asr server"))
    if s.mail_backend == "smtp":
        results.append(
            guarded("smtp", lambda: smtp_login(s, probes))
            if _needs_ok(offline, "mail")
            else skipped("smtp")
        )
    if s.notify_backend == "ntfy" and s.ntfy_url is not None:
        if _needs_ok(offline, "notify"):
            server, token = ntfy_checks(s, probes)
            results.append(server)
            if token is not None:
                results.append(token)
        else:
            results.append(skipped("ntfy server"))
    return results


def smtp_login(settings: Settings, probes: Probes) -> CheckResult:
    """Log in to the SMTP server and quit. Sends no MAIL, RCPT or DATA."""
    name = "smtp"
    options = SmtpOptions.from_settings(settings)
    mailer = SmtpMailer(
        options,
        socket_factory=probes.smtp_socket_factory,
        context_factory=probes.smtp_context_factory or DEFAULT_CONTEXT_FACTORY,
    )
    try:
        report = mailer.check_login(timeout_s=min(probes.timeout_s, options.timeout_s))
    except EarmarkError as exc:
        return CheckResult(name, "fail", exc.message)
    return CheckResult(
        name,
        "ok",
        f"{options.host}:{options.port}: {report.tls_version}, login accepted, nothing sent",
    )


def apple_lookup(settings: Settings, probes: Probes) -> CheckResult:
    name = "apple lookup"
    owned = probes.fetcher is None
    fetcher = probes.fetcher or GuardedFetcher(
        allow_private=settings.allow_private_fetch, timeout=_timeout(probes.timeout_s)
    )
    try:
        document = fetcher.get(
            LOOKUP_URL,
            params={"id": "1", "entity": "podcast"},
            max_bytes=MAX_BODY_BYTES,
            purpose="iTunes lookup",
            deadline_s=15.0,
        )
    except BlockedAddressError as exc:
        hint = ALLOW_PRIVATE_HINT
        if "EARMARK_ALLOW_PRIVATE_FETCH" in exc.message:
            hint = "; a filtering DNS resolver or the sandbox may be answering private addresses"
        return CheckResult(name, "fail", exc.message + hint)
    except EarmarkError as exc:
        return CheckResult(name, "fail", exc.message)
    finally:
        if owned:
            fetcher.close()
    try:
        data = json.loads(document.content)
    except ValueError:
        data = None
    if isinstance(data, dict) and "resultCount" in data:
        return CheckResult(name, "ok", "itunes.apple.com is reachable")
    return CheckResult(name, "warn", "itunes.apple.com answered, but not with the expected JSON")


def ntfy_checks(settings: Settings, probes: Probes) -> tuple[CheckResult, CheckResult | None]:
    options = NtfyOptions.from_settings(settings)
    if options is None:  # the caller checked EARMARK_NTFY_URL
        return CheckResult("ntfy server", "warn", "EARMARK_NTFY_URL is not set"), None
    host = options.host
    client = NtfyClient(options, transport=probes.http_transport, timeout_s=probes.timeout_s)
    try:
        server = guarded("ntfy server", lambda: _ntfy_server(client, host))
        token: CheckResult | None = None
        if options.token is not None and server.status != "fail":

            def account() -> CheckResult:
                try:
                    info = client.account()
                except NtfyAuthError:
                    return CheckResult(
                        "ntfy token", "fail", f"ntfy at {host} rejected EARMARK_NTFY_TOKEN"
                    )
                except EarmarkError as exc:
                    return CheckResult("ntfy token", "warn", exc.message)
                if info.messages_remaining == 0:
                    return CheckResult(
                        "ntfy token",
                        "warn",
                        "token accepted, but today's message quota is used up",
                    )
                return CheckResult("ntfy token", "ok", "token accepted")

            token = guarded("ntfy token", account)
        return server, token
    finally:
        client.close()


def _ntfy_server(client: NtfyClient, host: str) -> CheckResult:
    name = "ntfy server"
    try:
        health = client.health()
    except EarmarkError as exc:
        return CheckResult(name, "fail", exc.message)
    if health.status == 200 and health.healthy is True:
        return CheckResult(name, "ok", f"ntfy at {host} is reachable")
    if health.status == 503 or (health.status == 200 and health.healthy is False):
        return CheckResult(name, "warn", f"ntfy at {host} reports itself unhealthy")
    if health.status == 404:
        return CheckResult(
            name,
            "warn",
            f"ntfy at {host} answered, but has no /v1/health (ntfy older than 2.x?)",
        )
    if health.status == 200:
        return CheckResult(name, "warn", f"ntfy at {host} answered, but not with a health report")
    return CheckResult(name, "warn", f"ntfy at {host} answered HTTP {health.status}")
