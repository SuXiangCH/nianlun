"""Version-one router composition, matching diting-server's pattern."""

from fastapi import APIRouter

from app.api_server.apis.v1.apps.api import router as apps_router
from app.api_server.apis.v1.chat.api import router as chat_router
from app.api_server.apis.v1.healthz.api import router as health_router
from app.api_server.apis.v1.knowledge_bases.api import (
    router as knowledge_bases_router,
)
from app.api_server.apis.v1.models.api import router as models_router

v1_router = APIRouter()
# FastAPI 0.135+ keeps nested includes as lazy ``_IncludedRouter`` objects.
# Flatten the version router so the application exposes concrete APIRoutes and
# OpenAPI generation remains compatible across FastAPI minor versions.
for child_router in (
    health_router,
    knowledge_bases_router,
    apps_router,
    chat_router,
    models_router,
):
    v1_router.routes.extend(child_router.routes)

__all__ = ["v1_router"]
