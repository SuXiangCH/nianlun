"""Confined local storage; business locks and authorization belong to services."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

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


class FilesystemImageBlobStore:
    def __init__(
        self,
        root: Path,
        *,
        profile_id: str,
        max_bytes: int = MAX_IMAGE_BYTES,
        max_pixels: int = MAX_IMAGE_PIXELS,
    ) -> None:
        if max_bytes < 1 or max_pixels < 1:
            raise ValueError("Image limits must be positive")
        self.root = root.resolve()
        self.profile_id = profile_id
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels

    def _check_location(self, location: ImageBlobLocation) -> None:
        if (
            location.backend != "filesystem"
            or location.storage_profile_id != self.profile_id
        ):
            raise BlobStoreError("invalid_location")

    @contextmanager
    def _parent(
        self, location: ImageBlobLocation, *, create: bool = False
    ) -> Generator[tuple[int, str], None, None]:
        self._check_location(location)
        descriptor: int | None = None
        try:
            if create:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            parts = location.object_key.split("/")
            # Walk relative to directory descriptors so intermediate symlinks cannot escape.
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        except FileNotFoundError:
            raise BlobStoreError("image_missing") from None
        except PermissionError:
            raise BlobStoreError("storage_permission_denied") from None
        except OSError:
            raise BlobStoreError("storage_unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read(self, parent: int, name: str, metadata: ImageMetadata) -> bytes:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != metadata.size_bytes:
                raise BlobStoreError("integrity_error")
            data = source.read(metadata.size_bytes + 1)
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
        self._check_location(location)
        verify_image_bytes(
            data, metadata, max_bytes=self.max_bytes, max_pixels=self.max_pixels
        )
        with self._parent(location, create=True) as (parent, name):
            temporary = f".{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode=0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    # Atomic publication without replacing any immutable existing object.
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    self._read(parent, name, metadata)
                os.fsync(parent)
            finally:
                os.unlink(temporary, dir_fd=parent)
        return location

    @contextmanager
    def open(
        self, location: ImageBlobLocation, metadata: ImageMetadata
    ) -> Generator[BinaryIO, None, None]:
        with self._parent(location) as (parent, name):
            data = self._read(parent, name, metadata)
        with BytesIO(data) as source:
            yield source

    def stat(self, location: ImageBlobLocation) -> ImageBlobStat:
        with self._parent(location) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise BlobStoreError("invalid_location")
            return ImageBlobStat(size_bytes=info.st_size)

    def delete(
        self, location: ImageBlobLocation, *, all_versions: bool = False
    ) -> None:
        del all_versions
        try:
            with self._parent(location) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise BlobStoreError("invalid_location")
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
        except BlobStoreError as exc:
            if exc.reason != "image_missing":
                raise
