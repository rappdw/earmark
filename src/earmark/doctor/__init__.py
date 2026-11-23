"""``earmark doctor``: diagnose the configuration, directories, database, and backends.

Offline checks make no network call, load no model, download nothing, and create nothing
but two probe files that they remove at once. ``online=True`` adds reachability checks.
Import this package only from the CLI's ``doctor`` command: it loads httpx2.
"""

from dataclasses import dataclass
from typing import Final

import httpx2

from earmark.config import Settings
from earmark.doctor.report import CheckResult
from earmark.fetch import GuardedFetcher
from earmark.transcribe.compat import Lookup, installed

ONLINE_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class Probes:
    """Injection points for tests; production uses the defaults."""

    http_transport: httpx2.BaseTransport | None = None  # ntfy, ASR
    fetcher: GuardedFetcher | None = None  # the iTunes lookup
    timeout_s: float = ONLINE_TIMEOUT_S
    installed: Lookup = installed  # package metadata, for the asr libraries check


def run_doctor(
    settings: Settings, *, online: bool = False, probes: Probes | None = None
) -> list[CheckResult]:
    from earmark.doctor.checks import OfflineChecks
    from earmark.doctor.online import run_online

    chosen = probes or Probes()
    checks = OfflineChecks(settings, chosen)
    try:
        results = checks.run()
        if online:
            results.extend(run_online(settings, chosen, results, checks))
    finally:
        checks.close()
    return results
