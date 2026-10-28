"""Immutable storage configuration and dispatch for retained image profiles."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.api_server.storage.filesystem_image_blob_store import FilesystemImageBlobStore
from app.api_server.storage.image_blob_store import (
    BlobStoreError,
    ImageBlobLocation,
    ImageBlobStat,
)
from app.api_server.storage.image_validation import ImageMetadata
from app.api_server.storage.s3_image_blob_store import (
    S3ImageBlobStore,
    S3ImageStoreConfig,
)
from nianlun.config import PROJECT_ROOT

if TYPE_CHECKING:
    from app.api_server.config.settings import ApiServerSettings


class FilesystemImageStoreConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )
    backend: Literal["filesystem"] = "filesystem"
    root: Path


ProfileConfig = FilesystemImageStoreConfig | S3ImageStoreConfig


def _root(value: object) -> Path:
    if (
        not isinstance(value, (str, Path))
        or not str(value).strip()
        or "\x00" in str(value)
    ):
        raise ValueError("Invalid image storage root")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def normalize_profiles(
    settings: ApiServerSettings,
) -> tuple[str, Mapping[str, ProfileConfig]]:
    """Reject invalid configuration without exposing supplied values or secrets."""
    try:
        backend = settings.answer_image_storage_backend
        if backend not in {"filesystem", "s3"}:
            raise ValueError("backend")
        raw = settings.answer_image_storage_profiles
        if not isinstance(raw, Mapping):
            raise ValueError("profiles")
        profiles: dict[str, ProfileConfig] = {}
        for profile_id, entry in raw.items():
            if not isinstance(profile_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", profile_id
            ):
                raise ValueError("profile ID")
            if isinstance(entry, (FilesystemImageStoreConfig, S3ImageStoreConfig)):
                config = entry
                if isinstance(config, FilesystemImageStoreConfig):
                    config = FilesystemImageStoreConfig(root=_root(config.root))
                if (
                    isinstance(config, S3ImageStoreConfig)
                    and config.profile_id != profile_id
                ):
                    raise ValueError("profile ID")
            elif isinstance(entry, Mapping):
                values = dict(entry)
                kind = values.pop("backend", None)
                if kind == "filesystem":
                    values["root"] = _root(values.get("root"))
                    config = FilesystemImageStoreConfig.model_validate(values)
                elif kind == "s3" and "profile_id" not in values:
                    config = S3ImageStoreConfig(profile_id=profile_id, **values)
                else:
                    raise ValueError("profile backend")
            else:
                raise ValueError("profile")
            profiles[profile_id] = config
        selected = settings.answer_image_storage_profile_id
        if selected is not None and (
            not isinstance(selected, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", selected)
        ):
            raise ValueError("selected profile ID")
        if not profiles:
            selected = selected or f"{backend}-default"
            if backend == "filesystem":
                profiles[selected] = FilesystemImageStoreConfig(
                    root=_root(settings.data_dir / "answer-images")
                )
            else:
                profiles[selected] = S3ImageStoreConfig(
                    profile_id=selected,
                    bucket=settings.answer_image_s3_bucket,
                    region=settings.answer_image_s3_region,
                    endpoint_url=settings.answer_image_s3_endpoint_url,
                    addressing_style=settings.answer_image_s3_addressing_style,
                    access_key_id=settings.answer_image_s3_access_key_id,
                    secret_access_key=settings.answer_image_s3_secret_access_key,
                    session_token=settings.answer_image_s3_session_token,
                    ca_bundle=settings.answer_image_s3_ca_bundle,
                    connect_timeout_seconds=settings.answer_image_s3_connect_timeout_seconds,
                    read_timeout_seconds=settings.answer_image_s3_read_timeout_seconds,
                    total_max_attempts=settings.answer_image_s3_total_max_attempts,
                )
        if selected not in profiles:
            raise ValueError("selected profile")
        for profile_id, config in profiles.items():
            if isinstance(config, S3ImageStoreConfig) and config.ca_bundle is not None:
                profiles[profile_id] = config.model_copy(
                    update={"ca_bundle": str(_root(config.ca_bundle))}
                )
        actual_backend = (
            "filesystem"
            if isinstance(profiles[selected], FilesystemImageStoreConfig)
            else "s3"
        )
        if actual_backend != backend:
            raise ValueError("selected backend")
        return selected, MappingProxyType(profiles)
    except (ValueError, TypeError, OSError, ValidationError):
        raise ValueError("Invalid answer image storage configuration") from None


@dataclass(frozen=True)
class StorageProfileLocator:
    backend: Literal["filesystem", "s3"]
    canonical_json: str
    fingerprint: str


class ImageStorageRegistry:
    def __init__(self, settings: ApiServerSettings) -> None:
        self.selected_profile_id, self.profiles = normalize_profiles(settings)
        self.selected_backend = settings.answer_image_storage_backend
        self.max_bytes = settings.answer_image_max_image_bytes
        self.max_pixels = settings.answer_image_max_image_pixels
        locators = {}
        for profile_id, config in self.profiles.items():
            if isinstance(config, FilesystemImageStoreConfig):
                backend = "filesystem"
                locator = {"backend": backend, "root": str(config.root)}
            else:
                backend = "s3"
                locator = {
                    "backend": backend,
                    "endpoint_url": config.endpoint_url,
                    "bucket": config.bucket,
                    "region": config.region,
                    "addressing_style": config.addressing_style,
                    "object_prefix": "answer-images",
                }
            canonical = json.dumps(locator, sort_keys=True, separators=(",", ":"))
            locators[profile_id] = StorageProfileLocator(
                backend, canonical, hashlib.sha256(canonical.encode()).hexdigest()
            )
        self.locators = MappingProxyType(locators)
        self._stores: dict[str, FilesystemImageBlobStore | S3ImageBlobStore] = {}
        self._lock = threading.Lock()
        self._closed = False

    def _store(
        self, location: ImageBlobLocation
    ) -> FilesystemImageBlobStore | S3ImageBlobStore:
        with self._lock:
            if self._closed:
                raise BlobStoreError("storage_unavailable")
            locator = self.locators.get(location.storage_profile_id)
            if locator is None or locator.backend != location.backend:
                raise BlobStoreError("invalid_location")
            if location.storage_profile_id not in self._stores:
                config = self.profiles[location.storage_profile_id]
                self._stores[location.storage_profile_id] = (
                    FilesystemImageBlobStore(
                        config.root,
                        profile_id=location.storage_profile_id,
                        max_bytes=self.max_bytes,
                        max_pixels=self.max_pixels,
                    )
                    if isinstance(config, FilesystemImageStoreConfig)
                    else S3ImageBlobStore(
                        config, max_bytes=self.max_bytes, max_pixels=self.max_pixels
                    )
                )
            return self._stores[location.storage_profile_id]

    def put(
        self, location: ImageBlobLocation, data: bytes, metadata: ImageMetadata
    ) -> ImageBlobLocation:
        return self._store(location).put(location, data, metadata)

    def open(
        self, location: ImageBlobLocation, metadata: ImageMetadata
    ) -> AbstractContextManager[BinaryIO]:
        return self._store(location).open(location, metadata)

    def stat(self, location: ImageBlobLocation) -> ImageBlobStat:
        return self._store(location).stat(location)

    def delete(
        self, location: ImageBlobLocation, *, all_versions: bool = False
    ) -> None:
        self._store(location).delete(location, all_versions=all_versions)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with ExitStack() as shutdown:
                for store in self._stores.values():
                    if isinstance(store, S3ImageBlobStore):
                        shutdown.callback(store.close)


def build_image_storage_registry(settings: ApiServerSettings) -> ImageStorageRegistry:
    return ImageStorageRegistry(settings)
