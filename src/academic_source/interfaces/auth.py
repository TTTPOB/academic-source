"""Static Bearer authentication for the entire HTTP application."""

from hmac import compare_digest

from pydantic import SecretStr
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, *, api_keys: list[SecretStr]) -> None:
        self.app = app
        self.keys = tuple(key.get_secret_value().encode("ascii") for key in api_keys)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path) :]
        if (scope["method"] == "GET" and path == "/api/v1/status") or path.startswith(
            "/downloads/"
        ):
            # The download route validates its own single-use capability and methods.
            await self.app(scope, receive, send)
            return
        headers = [
            value
            for name, value in scope["headers"]
            if name.lower() == b"authorization"
        ]
        authorized = False
        if len(headers) == 1:
            scheme, separator, token = headers[0].partition(b" ")
            if scheme.lower() == b"bearer" and separator and token:
                for key in self.keys:
                    authorized |= compare_digest(token, key)
        if not authorized:
            response = JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
