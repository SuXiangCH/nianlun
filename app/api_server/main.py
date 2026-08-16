"""FastAPI application factory for Nianlun."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware

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
from app.api_server.services.container import ApiServices, build_services
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
            for service_name in ("chat", "documents", "fts", "vector"):
                service = getattr(active_services, service_name)
                try:
                    service.shutdown()
                except Exception:
                    logger.exception("service.shutdown_failed service=%s", service_name)

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
    )
    app.add_middleware(RequestTrackingMiddleware)
    app.include_router(v1_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


__all__ = ["app", "create_app"]
