"""Bearer-token authentication as a pure ASGI middleware.

It runs before routing and body parsing, so an unauthenticated request gets 401
whatever its path or body.
"""

import hashlib
import hmac
import logging
from typing import Final

from pydantic import SecretStr
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from earmark.config import Settings

logger = logging.getLogger(__name__)

PUBLIC_PATHS: Final = frozenset({"/healthz"})
WWW_AUTHENTICATE: Final = 'Bearer realm="earmark"'
UNAUTHORIZED_DETAIL: Final = "missing or invalid API token"


class ApiTokenError(ValueError):
    """The configured token is unusable. str(exc) names the variable, never the value."""


def require_api_token(settings: Settings) -> SecretStr:
    token = settings.api_token
    if token is None or not token.get_secret_value().strip():
        raise ApiTokenError(
            "EARMARK_API_TOKEN is not set; refusing to serve the API without a token. "
            "Set it in the environment or in .env (for example to the output of: "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))')."
        )
    if any(not "!" <= c <= "~" for c in token.get_secret_value()):
        raise ApiTokenError(
            "EARMARK_API_TOKEN must be printable ASCII without spaces, because it is sent "
            "in an HTTP header."
        )
    return token


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


def bearer_credentials(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return rest.strip() or None


def token_matches(presented: str, expected_digest: bytes) -> bool:
    return hmac.compare_digest(token_digest(presented), expected_digest)


class BearerAuthMiddleware:
    def __init__(
        self, app: ASGIApp, *, token: SecretStr, public_paths: frozenset[str] = PUBLIC_PATHS
    ) -> None:
        self.app = app
        self._digest = token_digest(token.get_secret_value())
        self._public_paths = public_paths

    def _rejection(self, scope: Scope) -> str | None:
        """Why the request is not authorised, or None when it is."""
        credentials = bearer_credentials(Headers(scope=scope).get("authorization"))
        if credentials is None:
            return "no bearer token"
        if not token_matches(credentials, self._digest):
            return "wrong token"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "http":
            if scope["path"] in self._public_paths:
                await self.app(scope, receive, send)
                return
            reason = self._rejection(scope)
            if reason is not None:
                client = scope.get("client")
                logger.warning(
                    "rejected %s %s from %s: %s",
                    scope["method"],
                    scope["path"],
                    client[0] if client else "unknown",
                    reason,
                )
                response = JSONResponse(
                    {"detail": UNAUTHORIZED_DETAIL},
                    status_code=401,
                    headers={"WWW-Authenticate": WWW_AUTHENTICATE},
                )
                await response(scope, receive, send)
                return
        elif kind == "websocket" and self._rejection(scope) is not None:
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        await self.app(scope, receive, send)
