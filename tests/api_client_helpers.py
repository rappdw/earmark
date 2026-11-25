"""Shared helpers for the CLI's client-mode tests (step 34)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.config import Settings
from tests.factories import TO

CLIENT_TOKEN = "client-token-SENTINEL-4f2a9c"
API_URL = "https://earmark.example.com"


def api_settings(data_dir: Path, *, token: str = CLIENT_TOKEN, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        api_token=SecretStr(token),
        data_dir=data_dir,
        allowed_recipients=(TO,),
        **overrides,
    )


def in_process_transport(app: FastAPI) -> httpx2.BaseTransport:
    """A transport that calls the app directly: real routes, no socket."""
    transport = TestClient(app)._transport
    assert isinstance(transport, httpx2.BaseTransport)
    return transport


class RecordingTransport(httpx2.BaseTransport):
    """Wraps another transport and records (method, url, headers, body) for each request."""

    def __init__(self, inner: httpx2.BaseTransport) -> None:
        self.inner = inner
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        body = request.read()
        self.requests.append((request.method, str(request.url), dict(request.headers), body))
        return self.inner.handle_request(request)


def use_transport(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], httpx2.BaseTransport]
) -> list[int]:
    """Make ApiClient's default transport come from ``factory``; the list counts the calls."""
    calls: list[int] = []

    def make() -> httpx2.BaseTransport:
        calls.append(1)
        return factory()

    monkeypatch.setattr("earmark.api_client.default_transport", make)
    return calls
