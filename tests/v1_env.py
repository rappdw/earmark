"""A wired /v1 app and worker for the /v1 tests: the real API over the fakes."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.config import Settings
from earmark.fakes import FakeBackends, FakeTranscriber, make_fake_backends
from earmark.jobs import JobStore
from earmark.models import Job, Segment
from earmark.pipeline.stages import Backends, Mailer, Notifier, OutgoingMail
from tests.factories import MutableClock
from tests.multipart_helpers import audio
from tests.pipeline_env import PipelineEnv, make_env

TOKEN = "v1-test-token-9d2c"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class CalledError(BaseException):
    """Raised by the mailer and notifier below: a BaseException, so no runner handler hides it."""


class ForbiddenMailer:
    sender = "earmark@example.org"

    def send(self, mail: OutgoingMail, /) -> None:
        raise CalledError("the mailer was called")


class ForbiddenNotifier:
    def notify(self, job: Job, episode: Any, /) -> None:
        raise CalledError("the notifier was called")


class NoDedupeStore(JobStore):
    """A store whose dedupe window is zero: an identical request is always a new job."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["dedupe_window"] = timedelta(0)
        super().__init__(*args, **kwargs)


@dataclass
class V1Env:
    env: PipelineEnv
    client: TestClient
    data_dir: Path

    @property
    def fakes(self) -> FakeBackends:
        return self.env.fakes

    @property
    def clock(self) -> MutableClock:
        return self.env.clock

    @property
    def jobs(self) -> JobStore:
        return self.env.jobs

    @property
    def uploads(self) -> list[str]:
        directory = self.data_dir / "uploads"
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def post(
        self,
        *,
        payload: bytes | None = None,
        filename: str = "meeting.flac",
        content_type: str = "audio/flac",
        **fields: Any,
    ) -> Any:
        return self.client.post(
            "/v1/transcriptions",
            files={"file": (filename, audio(64) if payload is None else payload, content_type)},
            data=fields,
        )

    def submit(self, **kwargs: Any) -> str:
        response = self.post(**kwargs)
        assert response.status_code == 202, response.text
        return str(response.json()["job_id"])

    def run(self) -> Job:
        job = self.env.worker.run_once()
        assert job is not None
        return job

    def run_all(self) -> list[Job]:
        done: list[Job] = []
        while (job := self.env.worker.run_once()) is not None:
            done.append(job)
        return done

    def transcript(self, job_id: str, fmt: str | None = None) -> Any:
        suffix = "" if fmt is None else f"?format={fmt}"
        return self.client.get(f"/v1/jobs/{job_id}/transcript{suffix}")


@contextmanager
def v1_env(
    tmp_path: Path,
    *,
    forbid_delivery: bool = True,
    no_dedupe: bool = False,
    fakes: FakeBackends | None = None,
    max_upload_bytes: int | None = None,
    allowed_recipients: tuple[str, ...] = (),
    segments: Sequence[Segment] | None = None,
    backends_hook: Callable[[Backends], Backends] | None = None,
    settings_overrides: dict[str, Any] | None = None,
    **env_kwargs: Any,
) -> Iterator[V1Env]:
    fakes = fakes or make_fake_backends(tmp_path / "data")
    if segments is not None:
        fakes.transcriber = FakeTranscriber(fakes.artifacts, segments=segments)
    backends: Backends = fakes.backends()
    if forbid_delivery:
        mailer: Mailer = ForbiddenMailer()
        notifier: Notifier = ForbiddenNotifier()
        backends = replace(backends, mailer=mailer, notifier=notifier)
    if backends_hook is not None:
        backends = backends_hook(backends)
    env = make_env(
        tmp_path,
        fakes=fakes,
        backends=backends,
        store_type=NoDedupeStore if no_dedupe else JobStore,
        **env_kwargs,
    )
    settings = Settings(
        _env_file=None,
        api_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        allowed_recipients=allowed_recipients,
        **(settings_overrides or {}),
    )
    app = create_app(settings, store=env.jobs, max_upload_bytes=max_upload_bytes)
    with TestClient(app, headers=AUTH) as client:
        yield V1Env(env, client, tmp_path / "data")
