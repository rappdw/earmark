"""Doctor results and how they are printed. Standard library plus ``earmark.log`` only."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from earmark.config import Settings
from earmark.log import redact

type Status = Literal["ok", "warn", "fail", "skip"]

LABELS: Final[dict[Status, str]] = {"ok": "ok", "warn": "warn", "fail": "FAIL", "skip": "skip"}
MAX_LINE_DETAIL: Final = 300


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str


def doctor_secrets(settings: Settings) -> list[str]:
    """Every secret value to redact: the settings' secrets plus the bare ntfy topic.

    The full topic URL is a secret, and a server that echoes only the topic must not leak it.
    """
    values = settings.secret_values()
    if settings.ntfy_url is not None:
        topic = settings.ntfy_url.get_secret_value().rstrip("/").rpartition("/")[2]
        if topic:
            values.append(topic)
    return values


def render_line(result: CheckResult, secrets: Sequence[str]) -> str:
    """One output line: label, name, then the detail redacted, printable, and capped."""
    detail = redact(result.detail, secrets)
    detail = "".join(c if c.isprintable() else "?" for c in detail)
    if len(detail) > MAX_LINE_DETAIL:
        detail = detail[: MAX_LINE_DETAIL - 1] + "…"
    name = "".join(c if c.isprintable() else "?" for c in result.name)
    return f"{LABELS[result.status]:<5} {name:<14} {detail}"


def render(results: Sequence[CheckResult], secrets: Sequence[str]) -> list[str]:
    return [render_line(r, secrets) for r in results]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def summary(results: Sequence[CheckResult]) -> str:
    failed = sum(1 for r in results if r.status == "fail")
    warned = sum(1 for r in results if r.status == "warn")
    if failed:
        text = f"doctor: {_plural(failed, 'check')} failed"
        if warned:
            text += f", {_plural(warned, 'warning')}"
        return text
    if warned:
        return f"doctor: all checks passed, {_plural(warned, 'warning')}"
    return "doctor: all checks passed"


def exit_code(results: Sequence[CheckResult]) -> int:
    """1 if any check failed, else 0 (warnings are allowed)."""
    return 1 if any(r.status == "fail" for r in results) else 0
