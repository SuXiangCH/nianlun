"""Liveness endpoint."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, status

from app.api_server.common.errors import success

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/healthz")
def health_check() -> dict[str, Any]:
    return success(
        {
            "version": "v1",
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "code": status.HTTP_200_OK,
        }
    )


__all__ = ["router"]
