"""Bounded S3 IO; callers coordinate immutable keys, leases, and transactions."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from io import BytesIO
from typing import Any, BinaryIO, Literal
from urllib.parse import urlsplit

from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.api_server.storage.image_blob_store import (
    BlobStoreError,
    ImageBlobLocation,
    ImageBlobStat,
    verify_image_bytes,
)
from app.api_server.storage.image_validation import (
    ImageMetadata,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
)


class S3ImageStoreConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    region: str = Field(min_length=1, max_length=128)
    endpoint_url: str | None = None
    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    access_key_id: SecretStr | None = Field(default=None, repr=False)
    secret_access_key: SecretStr | None = Field(default=None, repr=False)
    session_token: SecretStr | None = Field(default=None, repr=False)
    ca_bundle: str | None = None
    connect_timeout_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    read_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    total_max_attempts: int = Field(default=3, ge=1)
    max_delete_pages: int = Field(default=16, ge=1, le=1000)

    @model_validator(mode="after")
    def safe_configuration(self) -> S3ImageStoreConfig:
        if self.endpoint_url is not None:
            parsed = urlsplit(self.endpoint_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Invalid object storage endpoint")
        if bool(self.access_key_id) != bool(self.secret_access_key) or (
            self.session_token is not None and self.access_key_id is None
        ):
            raise ValueError("Object storage credentials must be configured together")
        if any(
            value is not None and not value.get_secret_value()
            for value in (
                self.access_key_id,
                self.secret_access_key,
                self.session_token,
            )
        ):
            raise ValueError("Object storage credentials cannot be empty")
        return self


class S3ImageBlobStore:
    def __init__(
        self,
        config: S3ImageStoreConfig,
        *,
        client: Any = None,
        max_bytes: int = MAX_IMAGE_BYTES,
        max_pixels: int = MAX_IMAGE_PIXELS,
    ) -> None:
        if max_bytes < 1 or max_pixels < 1:
            raise ValueError("Image limits must be positive")
        self.config = config
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        self._client = client
        self._client_lock = threading.Lock()
        self._closed = False

    def _sdk(self) -> Any:
        with self._client_lock:
            if self._closed:
                raise BlobStoreError("storage_unavailable")
            if self._client is None:
                import boto3

                credentials = {}
                for name, value in (
                    ("aws_access_key_id", self.config.access_key_id),
                    ("aws_secret_access_key", self.config.secret_access_key),
                    ("aws_session_token", self.config.session_token),
                ):
                    if value is not None:
                        credentials[name] = value.get_secret_value()
                self._client = boto3.client(
                    "s3",
                    region_name=self.config.region,
                    endpoint_url=self.config.endpoint_url,
                    verify=self.config.ca_bundle or True,
                    config=Config(
                        signature_version="s3v4",
                        connect_timeout=self.config.connect_timeout_seconds,
                        read_timeout=self.config.read_timeout_seconds,
                        retries={
                            "mode": "standard",
                            "total_max_attempts": self.config.total_max_attempts,
                        },
                        s3={"addressing_style": self.config.addressing_style},
                    ),
                    **credentials,
                )
            return self._client

    @contextmanager
    def _errors(self) -> Generator[None, None, None]:
        try:
            yield
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "NoSuchVersion", "NotFound", "404"}:
                reason = "image_missing"
            elif code in {
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "ExpiredToken",
                "403",
            }:
                reason = "storage_permission_denied"
            else:
                reason = "storage_unavailable"
            raise BlobStoreError(reason) from None
        except (NoCredentialsError, PartialCredentialsError):
            raise BlobStoreError("storage_permission_denied") from None
        except (BotoCoreError, OSError):
            raise BlobStoreError("storage_unavailable") from None

    def _params(self, location: ImageBlobLocation) -> dict[str, str]:
        if (
            location.backend != "s3"
            or location.storage_profile_id != self.config.profile_id
        ):
            raise BlobStoreError("invalid_location")
        params = {"Bucket": self.config.bucket, "Key": location.object_key}
        if location.version_id is not None:
            params["VersionId"] = location.version_id
        return params

    def _read(self, location: ImageBlobLocation, metadata: ImageMetadata) -> bytes:
        params = self._params(location)
        with self._errors():
            response = self._sdk().get_object(**params)
            body = response["Body"]
            try:
                if response.get("ContentLength") != metadata.size_bytes:
                    raise BlobStoreError("integrity_error")
                if location.version_id is not None and response.get(
                    "VersionId"
                ) not in {None, location.version_id}:
                    raise BlobStoreError("integrity_error")
                data = body.read(metadata.size_bytes + 1)
            finally:
                body.close()
        verify_image_bytes(
            data,
            metadata,
            max_bytes=metadata.size_bytes,
            max_pixels=metadata.width * metadata.height,
        )
        return data

    def put(
        self, location: ImageBlobLocation, data: bytes, metadata: ImageMetadata
    ) -> ImageBlobLocation:
        params = self._params(location)
        if location.version_id is not None:
            raise BlobStoreError("invalid_location")
        verify_image_bytes(
            data, metadata, max_bytes=self.max_bytes, max_pixels=self.max_pixels
        )
        with self._errors():
            response = self._sdk().put_object(
                **params,
                Body=data,
                ContentType=metadata.media_type,
                Metadata={"sha256": metadata.sha256},
            )
        version = response.get("VersionId")
        uploaded = ImageBlobLocation(
            backend="s3",
            storage_profile_id=location.storage_profile_id,
            object_key=location.object_key,
            version_id=version,
        )
        self._read(uploaded, metadata)
        return uploaded

    @contextmanager
    def open(
        self, location: ImageBlobLocation, metadata: ImageMetadata
    ) -> Generator[BinaryIO, None, None]:
        data = self._read(location, metadata)
        with BytesIO(data) as source:
            yield source

    def stat(self, location: ImageBlobLocation) -> ImageBlobStat:
        params = self._params(location)
        with self._errors():
            response = self._sdk().head_object(**params)
        return ImageBlobStat(
            size_bytes=response["ContentLength"],
            version_id=response.get("VersionId", location.version_id),
        )

    def delete(
        self, location: ImageBlobLocation, *, all_versions: bool = False
    ) -> None:
        params = self._params(location)
        try:
            with self._errors():
                client = self._sdk()
                if not all_versions:
                    client.delete_object(**params)
                    return
                # An unknown late PUT may create several versions; never delete a prefix.
                listing = {
                    "Bucket": self.config.bucket,
                    "Prefix": location.object_key,
                    "MaxKeys": 1000,
                }
                for _ in range(self.config.max_delete_pages):
                    page = client.list_object_versions(**listing)
                    for record in [
                        *page.get("Versions", []),
                        *page.get("DeleteMarkers", []),
                    ]:
                        if record["Key"] == location.object_key:
                            client.delete_object(
                                Bucket=self.config.bucket,
                                Key=location.object_key,
                                VersionId=record["VersionId"],
                            )
                    if not page.get("IsTruncated"):
                        return
                    listing.update(
                        KeyMarker=page["NextKeyMarker"],
                        VersionIdMarker=page["NextVersionIdMarker"],
                    )
                # Partial deletion is retryable; a GC pass must never loop forever.
                raise BlobStoreError("storage_unavailable")
        except BlobStoreError as exc:
            if exc.reason != "image_missing":
                raise

    def close(self) -> None:
        with self._client_lock:
            self._closed = True
            if self._client is not None:
                with self._errors():
                    self._client.close()
                self._client = None
