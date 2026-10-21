"""Private management endpoints for application publishing and API access."""

from typing import Any, Literal

from fastapi import APIRouter, Query, Request

from app.api_server.apis.v1.publishing_schemas import (
    ApiClientCreateRequest,
    ApiClientUpdateRequest,
    ApiKeyCreateRequest,
    ApplicationPublishRequest,
    ApplicationReleaseCreateRequest,
    ApplicationDeployRequest,
    ApplicationRestoreRequest,
    ApplicationUpdateRequest,
    PublicationChannelCreateRequest,
    PublicationResponse,
    PublicationUpdateRequest,
    WecomChannelUpdateRequest,
)
from app.api_server.common.errors import ApiError, success
from app.api_server.services.application_release_service import (
    ApplicationReleaseService,
)
from app.api_server.services.application_wecom_service import ApplicationWecomService

router = APIRouter(prefix="/api/v1/apps", tags=["apps"])


def _service(request: Request) -> ApplicationReleaseService:
    return request.app.state.services.publishing


def _wecom(request: Request) -> ApplicationWecomService:
    service = getattr(request.app.state.services, "wecom", None)
    if service is None:
        raise ApiError("企业微信渠道未启用", 503)
    return service


def _attach(request: Request, publication: PublicationResponse) -> PublicationResponse:
    service = getattr(request.app.state.services, "wecom", None)
    return publication if service is None else service.attach(publication)


@router.patch("/{application_id}")
def update_application(
    application_id: str, body: ApplicationUpdateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request).update(application_id, body).model_dump(mode="json")
    )


@router.get("/{application_id}/publication")
def get_publication(application_id: str, request: Request) -> dict[str, Any]:
    publication = _service(request).get_publication(application_id)
    return success(_attach(request, publication).model_dump(mode="json"))


@router.post("/{application_id}/publish")
def publish(
    application_id: str, body: ApplicationPublishRequest, request: Request
) -> dict[str, Any]:
    publication = _service(request).publish(application_id, body)
    return success(_attach(request, publication).model_dump(mode="json"))


@router.post("/{application_id}/channels")
def create_channel(
    application_id: str, body: PublicationChannelCreateRequest, request: Request
) -> dict[str, Any]:
    if body.type == "api":
        publication = _service(request).create_api_channel(application_id)
        return success(_attach(request, publication).model_dump(mode="json"))
    return success(_wecom(request).create(application_id, body).model_dump(mode="json"))


@router.get("/{application_id}/channels/wecom")
def get_wecom_channel(application_id: str, request: Request) -> dict[str, Any]:
    publication = _service(request).get_publication(application_id)
    return success(_attach(request, publication).model_dump(mode="json"))


@router.put("/{application_id}/channels/wecom")
def update_wecom_channel(
    application_id: str, body: WecomChannelUpdateRequest, request: Request
) -> dict[str, Any]:
    return success(_wecom(request).update(application_id, body).model_dump(mode="json"))


@router.patch("/{application_id}/channels/wecom")
def set_wecom_enabled(
    application_id: str, body: PublicationUpdateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _wecom(request).set_enabled(application_id, body).model_dump(mode="json")
    )


@router.post("/{application_id}/channels/wecom/connect")
def connect_wecom_channel(application_id: str, request: Request) -> dict[str, Any]:
    return success(_wecom(request).connect(application_id))


@router.patch("/{application_id}/publication")
def set_enabled(
    application_id: str, body: PublicationUpdateRequest, request: Request
) -> dict[str, Any]:
    publication = _service(request).set_enabled(application_id, body)
    return success(_attach(request, publication).model_dump(mode="json"))


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


@router.post("/{application_id}/releases")
def create_release(
    application_id: str, body: ApplicationReleaseCreateRequest, request: Request
) -> dict[str, Any]:
    return success(
        _service(request).create_release(application_id, body).model_dump(mode="json")
    )


@router.get("/{application_id}/releases/{release_id}")
def get_release(
    application_id: str, release_id: str, request: Request
) -> dict[str, Any]:
    return success(
        _service(request)
        .require_release(application_id, release_id)
        .model_dump(mode="json")
    )


@router.post("/{application_id}/releases/{release_id}/restore")
def restore_release(
    application_id: str,
    release_id: str,
    body: ApplicationRestoreRequest,
    request: Request,
) -> dict[str, Any]:
    return success(
        _service(request)
        .restore_release(application_id, release_id, body)
        .model_dump(mode="json")
    )


@router.post("/{application_id}/deployments")
def deploy(
    application_id: str, body: ApplicationDeployRequest, request: Request
) -> dict[str, Any]:
    if body.channel_type == "wecom":
        return success(
            _wecom(request).deploy(application_id, body).model_dump(mode="json")
        )
    publication = _service(request).deploy(application_id, body)
    return success(_attach(request, publication).model_dump(mode="json"))


@router.get("/{application_id}/deployments")
def list_deployments(
    application_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    channel_type: Literal["api", "wecom"] = Query(default="api"),
) -> dict[str, Any]:
    return success(
        _service(request)
        .list_deployments(application_id, limit, offset, channel_type=channel_type)
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
