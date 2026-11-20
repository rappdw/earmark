"""Known-incompatible versions of the ``transcribe`` extra's packages, from metadata only.

Light imports: this never imports the packages it checks, so it is safe in doctor and
in the default test suite.
"""

import importlib.metadata
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class Installed:
    version: str
    from_source: bool  # direct_url.json has vcs_info or dir_info (PEP 610)


@dataclass(frozen=True, slots=True)
class Incompatibility:
    package: str  # distribution name, e.g. "faster-whisper"
    up_to: tuple[int, ...]  # affected releases of `package`: release <= up_to
    other: str  # e.g. "av"
    from_other: tuple[int, ...]  # affected releases of `other`: release >= from_other
    effect: str  # one clause: what breaks


@dataclass(frozen=True, slots=True)
class CompatResult:
    status: Literal["ok", "incompatible", "unknown"]
    detail: str


type Lookup = Callable[[str], Installed | None]

TRANSCRIBE_PACKAGES: Final = ("faster-whisper", "av")
KNOWN_INCOMPATIBLE: Final[tuple[Incompatibility, ...]] = (
    Incompatibility(
        package="faster-whisper",
        up_to=(1, 2, 1),
        other="av",
        from_other=(19,),
        effect=(
            "faster-whisper passes av.open(metadata_errors=...), which PyAV 19 removed, "
            "so every file fails to decode"
        ),
    ),
)
MAX_VERSION_CHARS: Final = 40
_RELEASE: Final = re.compile(r"\s*(?:\d+!)?(\d+(?:\.\d+)*)")


def installed(name: str) -> Installed | None:
    """The installed version of a distribution, or None when absent or unreadable."""
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    version: object = dist.version  # metadata is untrusted: check the type
    if not isinstance(version, str) or not version:
        return None
    from_source = False
    try:
        raw = dist.read_text("direct_url.json")
        if isinstance(raw, str):
            data = json.loads(raw)
            from_source = isinstance(data, dict) and ("vcs_info" in data or "dir_info" in data)
    except (OSError, ValueError):
        from_source = False
    return Installed(version, from_source)


def release(version: str) -> tuple[int, ...] | None:
    """The dotted release numbers with trailing zeros stripped; None when unparsable."""
    match = _RELEASE.match(version)
    if match is None:
        return None
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _shown(version: str) -> str:
    return " ".join(version.split())[:MAX_VERSION_CHARS]


def check_transcribe_packages(lookup: Lookup = installed) -> CompatResult:
    found = {name: lookup(name) for name in TRANSCRIBE_PACKAGES}
    releases: dict[str, tuple[int, ...]] = {}
    unreadable: list[str] = []
    for name, item in found.items():
        rel = None if item is None else release(item.version)
        if item is None or rel is None:
            unreadable.append(name)
        else:
            releases[name] = rel
    if unreadable:
        return CompatResult(
            "unknown",
            f"cannot read the installed version of {' and '.join(unreadable)}; "
            "not checked against known-incompatible pairs",
        )
    shown = {name: _shown(item.version) for name, item in found.items() if item is not None}
    sources = {name for name, item in found.items() if item is not None and item.from_source}
    for rule in KNOWN_INCOMPATIBLE:
        if rule.package in sources or rule.other in sources:
            continue
        if releases[rule.package] <= rule.up_to and releases[rule.other] >= rule.from_other:
            return CompatResult(
                "incompatible",
                f"{rule.package} {shown[rule.package]} with {rule.other} {shown[rule.other]} "
                f"is a known-incompatible pair: {rule.effect}; reinstall the locked versions "
                "with `uv sync --locked --extra transcribe`",
            )
    detail = ", ".join(f"{name} {shown[name]}" for name in TRANSCRIBE_PACKAGES)
    from_source = [name for name in TRANSCRIBE_PACKAGES if name in sources]
    if from_source:
        detail += (
            f"; {' and '.join(from_source)} installed from a source tree, "
            "so its version is not checked"
        )
    return CompatResult("ok", detail)
