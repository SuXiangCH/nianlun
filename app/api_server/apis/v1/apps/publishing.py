"""Private management endpoints for application publishing and API access."""

from typing import Any

from fastapi import APIRouter, Query, Request

from app.api_server.apis.v1.publishing_schemas import (
    ApiClientCreateRequest,
    ApiClientUpdateRequest,
    ApiKeyCreateRequest,
    ApplicationPublishRequest,
    ApplicationUpdateRequest,
    PublicationChannelCreateRequest,
    PublicationUpdateRequest,
)
from app.api_server.common.errors import success
from app.api_server.services.application_release_service import (
    ApplicationReleaseService,
)

router = APIRouter(prefix="/api/v1/apps", tags=["apps"])


def _service(request: Request) -> ApplicationReleaseService:
    return request.app.state.services.publishing


@router.patch("/{application_id}")
def update_application(
    application_id: str, body: ApplicationUpdateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request).update(application_id, body).model_dump(mode="json")
    )


@router.get("/{application_id}/publication")
def get_publication(application_id: str, request: Request) -> dict[str, Any]:
    return success(
        _service(request).get_publication(application_id).model_dump(mode="json")
    )


@router.post("/{application_id}/publish")
def publish(
    application_id: str, body: ApplicationPublishRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request).publish(application_id, body).model_dump(mode="json")
    )


@router.post("/{application_id}/channels")
def create_channel(
    application_id: str, body: PublicationChannelCreateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request).create_api_channel(application_id).model_dump(mode="json")
    )


@router.patch("/{application_id}/publication")
def set_enabled(
    application_id: str, body: PublicationUpdateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request).set_enabled(application_id, body).model_dump(mode="json")
    )


@router.get("/{application_id}/releases")
def list_releases(
    application_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return success(
        _service(request)
        .list_releases(application_id, limit, offset)
        .model_dump(mode="json")
    )


@router.post("/{application_id}/api-clients")
def create_client(
    application_id: str, body: ApiClientCreateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request).create_client(application_id, body).model_dump(mode="json")
    )


@router.get("/{application_id}/api-clients")
def list_clients(application_id: str, request: Request) -> dict[str, Any]:
    return success(
        [
            item.model_dump(mode="json")
            for item in _service(request).list_clients(application_id)
        ]
    )


@router.patch("/{application_id}/api-clients/{client_id}")
def update_client(
    application_id: str, client_id: str, body: ApiClientUpdateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request)
        .update_client(application_id, client_id, body)
        .model_dump(mode="json")
    )


@router.post("/{application_id}/api-clients/{client_id}/keys")
def register_key(
    application_id: str, client_id: str, body: ApiKeyCreateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request)
        .register_key(application_id, client_id, body)
        .model_dump(mode="json")
    )


@router.get("/{application_id}/api-clients/{client_id}/keys")
def list_keys(application_id: str, client_id: str, request: Request) -> dict[str, Any]:
    return success(
        [
            item.model_dump(mode="json")
            for item in _service(request).list_keys(application_id, client_id)
        ]
    )


@router.delete("/{application_id}/api-clients/{client_id}/keys/{key_id}")
def revoke_key(
    application_id: str, client_id: str, key_id: str, request: Request
) -> dict[str, Any]:
    return success(
        _service(request)
        .revoke_key(application_id, client_id, key_id)
        .model_dump(mode="json")
    )
