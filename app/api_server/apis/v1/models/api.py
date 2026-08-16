"""CRUD routes for the single-user model catalog."""

from typing import Any, Literal

from fastapi import APIRouter, Request

from app.api_server.apis.v1.schemas import ModelConfigTestRequest, ModelProfileRequest
from app.api_server.common.errors import success
from app.api_server.services.container import ApiServices

ModelKind = Literal["llm", "embedding", "parser"]
router = APIRouter(prefix="/api/v1/models", tags=["models"])


def _services(request: Request) -> ApiServices:
    return request.app.state.services


@router.get("")
def list_models(request: Request, kind: ModelKind | None = None) -> dict[str, Any]:
    items = _services(request).models.list_profiles(kind)
    return success([item.model_dump(mode="json") for item in items])


@router.post("")
def create_model(body: ModelProfileRequest, request: Request) -> dict[str, Any]:
    services = _services(request)
    item = services.models.create_profile(body)
    services.applications.invalidate_all()
    return success(item.model_dump(mode="json"))


@router.post("/test")
def test_model_draft(
    body: ModelConfigTestRequest, request: Request
) -> dict[str, Any]:
    """Test a model draft without persisting it."""
    item = _services(request).models.test_connection(body)
    return success(item.model_dump(mode="json"))


@router.get("/{model_id}")
def get_model(model_id: str, request: Request) -> dict[str, Any]:
    item = _services(request).models.get_profile(model_id)
    return success(item.model_dump(mode="json"))


@router.put("/{model_id}")
def update_model(
    model_id: str, body: ModelProfileRequest, request: Request
) -> dict[str, Any]:
    services = _services(request)
    item = services.models.update_profile(model_id, body)
    services.applications.invalidate_all()
    return success(item.model_dump(mode="json"))


@router.delete("/{model_id}")
def delete_model(model_id: str, request: Request) -> dict[str, Any]:
    _services(request).models.delete_profile(model_id)
    return success(None)


@router.post("/{model_id}/default")
def set_default_model(model_id: str, request: Request) -> dict[str, Any]:
    services = _services(request)
    item = services.models.set_default_profile(model_id)
    services.applications.invalidate_all()
    return success(item.model_dump(mode="json"))


@router.post("/{model_id}/test")
def test_model(
    model_id: str,
    request: Request,
    body: ModelConfigTestRequest | None = None,
) -> dict[str, Any]:
    item = _services(request).models.test_profile(model_id, body)
    return success(item.model_dump(mode="json"))


__all__ = ["router"]
