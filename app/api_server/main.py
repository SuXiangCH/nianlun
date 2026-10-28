"""FastAPI application factory for Nianlun."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api_server.apis.internal.router import (
    InternalNoStoreMiddleware,
    internal_router,
)
from app.api_server.apis.v1.router import v1_router
from app.api_server.common.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    internal_error_handler,
    validation_error_handler,
)
from app.api_server.common.logging import configure_logging
from app.api_server.config import ApiServerSettings, get_settings
from app.api_server.middleware import RequestTrackingMiddleware
from app.api_server.services.container import (
    ApiServices,
    build_services,
    shutdown_services,
)
from nianlun import __version__


logger = logging.getLogger(__name__)


def create_app(
    settings: ApiServerSettings | None = None,
    services: ApiServices | None = None,
) -> FastAPI:
    """Create an app; optional arguments make route tests deterministic."""
    active_settings = settings or get_settings()
    active_services = services or build_services(active_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            shutdown_services(active_services)

    try:
        return _configure_app(active_settings, active_services, lifespan)
    except BaseException:
        shutdown_services(active_services)
        raise


def _configure_app(
    active_settings: ApiServerSettings,
    active_services: ApiServices,
    lifespan: Any,
) -> FastAPI:

    configure_logging(active_settings.log_level)
    app = FastAPI(title="Nianlun API", version=__version__, lifespan=lifespan)
    app.state.settings = active_settings
    app.state.services = active_services
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Location", "X-Request-Id"],
    )
    app.add_middleware(RequestTrackingMiddleware)
    app.add_middleware(InternalNoStoreMiddleware)
    app.include_router(v1_router)
    app.include_router(internal_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)


__all__ = ["app", "create_app"]
