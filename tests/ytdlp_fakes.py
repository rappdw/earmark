"""A fake yt-dlp runtime: stand-in error classes and a scripted session, no yt-dlp import."""

import json
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from earmark.youtube.ytdlp import YtDlpErrors, YtDlpRuntime, YtDlpSession

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"


def fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class FakeYoutubeDLError(Exception):
    def __init__(self, msg: str = "") -> None:
        super().__init__(msg)
        self.msg = msg


class FakeExtractorError(FakeYoutubeDLError):
    def __init__(
        self,
        msg: str = "",
        *,
        expected: bool = False,
        cause: BaseException | None = None,
        video_id: str | None = None,
    ) -> None:
        super().__init__(msg)
        self.orig_msg = msg
        self.expected = expected
        self.cause = cause
        self.video_id = video_id


class FakeGeoRestrictedError(FakeExtractorError):
    pass


class FakeDownloadError(FakeYoutubeDLError):
    def __init__(self, msg: str = "", exc_info: tuple[Any, Any, Any] | None = None) -> None:
        super().__init__(msg)
        self.exc_info = exc_info


class FakeDownloadCancelled(  # noqa: N818  # mirrors the name of yt-dlp's class
    FakeYoutubeDLError
):
    pass


class FakeContentTooShortError(FakeYoutubeDLError):
    pass


class FakeRequestError(Exception):
    pass


class FakeTransportError(FakeRequestError):
    pass


class FakeIncompleteRead(  # noqa: N818  # mirrors the name of yt-dlp's class
    FakeTransportError
):
    pass


class FakeHTTPError(FakeRequestError):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP Error {status}")
        self.status = status


FAKE_ERRORS = YtDlpErrors(
    download_error=FakeDownloadError,
    extractor_error=FakeExtractorError,
    geo_restricted=FakeGeoRestrictedError,
    download_cancelled=FakeDownloadCancelled,
    content_too_short=FakeContentTooShortError,
    http_error=FakeHTTPError,
    transport_error=FakeTransportError,
)


def wrap(exc: BaseException) -> FakeDownloadError:
    """Wrap ``exc`` as ``YoutubeDL.trouble`` does: the original is ``exc_info[1]``."""
    return FakeDownloadError("ERROR: " + str(exc), exc_info=(type(exc), exc, None))


@dataclass
class Script:
    """What a fake session does. ``info`` is returned by ``extract_info``."""

    info: Any = None
    warnings: list[str] = field(default_factory=list)
    extract_error: BaseException | None = None
    # ``on_download`` runs first; then ``downloaded_bytes`` values go to each progress hook.
    progress: list[int] = field(default_factory=list)
    payload: bytes | None = b"fake audio\n"  # None writes nothing
    download_error: BaseException | None = None
    on_download: Callable[[dict[str, Any]], None] | None = None


@dataclass
class Call:
    kind: str  # "extract_info" or "process_ie_result"
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    params: dict[str, Any]


class FakeSession:
    def __init__(self, script: Script, params: dict[str, Any], calls: list[Call]) -> None:
        self.script = script
        self.params = params
        self.calls = calls
        self.closed = False

    def extract_info(self, url: str, *, download: bool, process: bool, ie_key: str) -> Any:
        self.calls.append(
            Call(
                "extract_info",
                (url,),
                {"download": download, "process": process, "ie_key": ie_key},
                self.params,
            )
        )
        for text in self.script.warnings:
            self.params["logger"].warning(text)
        if self.script.extract_error is not None:
            raise self.script.extract_error
        return self.script.info

    def process_ie_result(self, info: Any, *, download: bool) -> Any:
        self.calls.append(Call("process_ie_result", (info,), {"download": download}, self.params))
        if self.script.on_download is not None:
            self.script.on_download(self.params)
        for step in self.script.progress:
            for hook in self.params["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": step})
        if self.script.download_error is not None:
            raise self.script.download_error
        if self.script.payload is not None:
            target = Path(self.params["outtmpl"]["default"].replace("%%", "%"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.script.payload)
        return info

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeRuntime:
    runtime: YtDlpRuntime
    calls: list[Call]
    sessions: list[FakeSession]
    scripts: list[Script]

    def queue(self, *scripts: Script) -> None:
        """Scripts for the next sessions, in order; the last one repeats."""
        self.scripts.extend(scripts)

    @property
    def extracts(self) -> list[Call]:
        return [c for c in self.calls if c.kind == "extract_info"]

    @property
    def downloads(self) -> list[Call]:
        return [c for c in self.calls if c.kind == "process_ie_result"]


def fake_runtime(tmp_path: Path, *scripts: Script, deno: bool = True) -> FakeRuntime:
    """A ``YtDlpRuntime`` whose sessions follow ``scripts`` (the last repeats)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    deno_path = bin_dir / "deno"
    if deno:
        deno_path.write_text("#!/bin/sh\n", encoding="utf-8")
        deno_path.chmod(deno_path.stat().st_mode | stat.S_IXUSR)
    calls: list[Call] = []
    sessions: list[FakeSession] = []
    queue = list(scripts)

    def open_session(params: dict[str, Any]) -> YtDlpSession:
        script = queue[min(len(sessions), len(queue) - 1)]
        session = FakeSession(script, params, calls)
        sessions.append(session)
        return session

    runtime = YtDlpRuntime(
        version="2026.11.23",
        ejs_version="0.8.0",
        deno_version="2.9.8",
        deno_path=deno_path,
        errors=FAKE_ERRORS,
        open_session=open_session,
    )
    return FakeRuntime(runtime=runtime, calls=calls, sessions=sessions, scripts=queue)


def public_info(**changes: Any) -> dict[str, Any]:
    info = fixture("public.json")
    info.update(changes)
    return info


def refusal_info(name: str) -> tuple[dict[str, Any], list[str]]:
    data = fixture(name)
    return data["info"], list(data["warnings"])
