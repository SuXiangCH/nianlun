"""Application management routes."""

from typing import Any

from fastapi import APIRouter, Request

from app.api_server.apis.v1.schemas import ApplicationCreateRequest
from app.api_server.common.errors import success
from app.api_server.services.container import ApiServices

router = APIRouter(prefix="/api/v1/apps", tags=["apps"])


def _services(request: Request) -> ApiServices:
    return request.app.state.services


@router.post("")
def create_app(body: ApplicationCreateRequest, request: Request) -> dict[str, Any]:
    item = _services(request).applications.create(body)
    return success(item.model_dump(mode="json"))


@router.get("")
def list_apps(request: Request) -> dict[str, Any]:
    items = _services(request).applications.list()
    return success([item.model_dump(mode="json") for item in items])


@router.get("/{application_id}")
def get_app(application_id: str, request: Request) -> dict[str, Any]:
    item = _services(request).applications.get(application_id)
    return success(item.model_dump(mode="json"))


@router.delete("/{application_id}")
def delete_app(application_id: str, request: Request) -> dict[str, Any]:
    _services(request).applications.delete(application_id)
    return success(None)


__all__ = ["router"]
