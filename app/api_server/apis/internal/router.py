"""Internal router composition and cache-control enforcement."""

from fastapi import APIRouter
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api_server.apis.internal.wecom import router as wecom_router

internal_router = APIRouter()
for child_router in (wecom_router,):
    internal_router.routes.extend(child_router.routes)


class InternalNoStoreMiddleware:
    """Keep internal responses, including errors, out of shared caches."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(
            "/internal/"
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(raw=message["headers"])["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_no_store)


__all__ = ["InternalNoStoreMiddleware", "internal_router"]
