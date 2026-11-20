"""The FastAPI application factory and its error handlers."""

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from earmark import __version__
from earmark.api.auth import BearerAuthMiddleware, require_api_token
from earmark.api.routes import build_router
from earmark.config import Settings
from earmark.errors import EarmarkError, TransientError
from earmark.index.search import IndexMismatchError, InvalidQuery, Searcher, UnavailableSearcher
from earmark.jobs import JobStore, open_job_store
from earmark.models import INGEST_ERROR_TYPES, IngestRequest

logger = logging.getLogger(__name__)

MAX_DETAIL_CHARS: Final = 300
STORE_UNAVAILABLE_DETAIL: Final = "the job store is unavailable; try again shortly"
_OBJECT_TYPES: Final = frozenset({"model_attributes_type", "model_type", "dict_type"})


def _describe(error: Mapping[str, Any]) -> str:
    kind = error.get("type")
    loc = tuple(error.get("loc", ()))
    field = ".".join(str(p) for p in loc[1:])
    msg = str(error.get("msg", ""))
    if kind == "json_invalid":
        return "request body is not valid JSON"
    if kind == "missing" and loc == ("body",):
        return "request body is missing; send a JSON object with a url"
    if kind in _OBJECT_TYPES and loc == ("body",):
        return "request body must be a JSON object sent with Content-Type: application/json"
    if kind == "extra_forbidden" and loc[:1] == ("body",):
        allowed = ", ".join(IngestRequest.model_fields)
        return f"unknown field '{field[:40]}' (allowed: {allowed})"
    if kind == "missing":
        return f"{field} is required"
    if kind in INGEST_ERROR_TYPES:
        return msg
    if field == "timestamp":
        return f"timestamp: {msg} (send ISO 8601 with a UTC offset, e.g. 2026-11-19T19:03:03-08:00)"
    return f"{field}: {msg}" if field else msg


def describe_validation_errors(errors: Sequence[Mapping[str, Any]]) -> str:
    """One short string for a 422. Never uses the error's input or ctx."""
    messages = list(dict.fromkeys(_describe(e) for e in errors))
    text = "; ".join(messages)
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS] + "…"
    return text


async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise TypeError("unexpected exception type")
    return JSONResponse({"detail": describe_validation_errors(exc.errors())}, status_code=422)


async def _on_store_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, sqlite3.Error):
        raise TypeError("unexpected exception type")
    logger.error("job store error", exc_info=exc)
    return JSONResponse(
        {"detail": STORE_UNAVAILABLE_DETAIL}, status_code=503, headers={"Retry-After": "5"}
    )


async def _on_invalid_query(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, InvalidQuery):
        raise TypeError("unexpected exception type")
    return JSONResponse({"detail": str(exc)}, status_code=422)


async def _on_index_mismatch(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, IndexMismatchError):
        raise TypeError("unexpected exception type")
    return JSONResponse({"detail": exc.message}, status_code=409)


async def _on_search_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, EarmarkError):
        raise TypeError("unexpected exception type")
    headers = {"Retry-After": "5"} if isinstance(exc, TransientError) else None
    return JSONResponse({"detail": exc.message}, status_code=503, headers=headers)


def create_app(
    settings: Settings, *, store: JobStore | None = None, searcher: Searcher | None = None
) -> FastAPI:
    token = require_api_token(settings)
    if searcher is None:
        searcher = UnavailableSearcher("search is not configured in this process")
    store = store if store is not None else open_job_store(settings)
    app = FastAPI(
        title="earmark",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_oauth2_redirect_url=None,
        telemetry={
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        },
    )
    app.include_router(build_router(store, searcher))
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(sqlite3.Error, _on_store_error)
    app.add_exception_handler(InvalidQuery, _on_invalid_query)
    app.add_exception_handler(IndexMismatchError, _on_index_mismatch)
    app.add_exception_handler(EarmarkError, _on_search_error)
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app
