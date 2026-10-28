"""Build immutable, version-bound image catalogs from normalized Markdown."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api_server.services.workspace_store import WorkspaceArtifactStore
from app.api_server.storage.image_validation import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    ImageMetadata,
    ImageValidationError,
    validate_image,
)
from nianlun.knowledgebase.core import sanitize_text
from nianlun.knowledgebase.image_syntax import parse_image_syntax


class ImageCatalogOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str | None = None
    line_num: int = Field(ge=1)
    char_offset: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class ImageCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_id: str = Field(pattern=r"^img_[0-9a-f]{16,64}$")
    source: str = Field(min_length=1)
    asset_relpath: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(pattern=r"^image/(png|jpeg|webp)$")
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    orientation: int = Field(ge=1, le=8)
    alt_text: str = ""
    occurrences: tuple[ImageCatalogOccurrence, ...] = ()


class ImageCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: int = Field(default=1, ge=1, le=1)
    document_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[ImageCatalogEntry, ...] = ()
    unavailable: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageCatalogBuild:
    catalog: ImageCatalog | None
    files: dict[str, bytes]


def catalog_bundle_digest(
    entries: list[dict[str, Any]], unavailable: tuple[str, ...]
) -> str:
    normalized = [
        {key: value for key, value in item.items() if key != "asset_relpath"}
        for item in entries
    ]
    payload = {
        "format_version": 1,
        "entries": normalized,
        "unavailable": sorted(set(unavailable)),
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _safe_asset_path(root: Path, source: str) -> Path:
    split = urlsplit(source)
    if split.scheme or split.netloc or not split.path:
        raise ValueError("external image source")
    relative = PurePosixPath(unquote(split.path).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("image source escapes normalized artifact")
    path = (root / Path(*relative.parts)).resolve()
    path.relative_to(root.resolve())
    if not path.is_file():
        raise FileNotFoundError(source)
    return path


def _tree_nodes(tree: dict[str, object]):
    def walk(items: object):
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    yield item
                    yield from walk(item.get("nodes"))

    yield from walk(tree.get("structure"))


def build_image_catalog(
    workspace: Path,
    document_id: str,
    generation: int,
    markdown: bytes,
    tree: dict[str, object],
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> ImageCatalogBuild:
    """Validate normalized image references and return deterministic bundle files."""
    if (
        type(max_bytes) is not int
        or type(max_pixels) is not int
        or min(max_bytes, max_pixels) < 1
    ):
        raise ValueError("Image limits must be positive integers")
    text = markdown.decode("utf-8")
    normalized_root = (
        workspace / "artifacts" / document_id / f"g{generation}" / "normalized"
    ).resolve()
    occurrences: dict[str, list[ImageCatalogOccurrence]] = {}
    metadata: dict[str, tuple[str, ImageMetadata, bytes, str]] = {}
    unavailable: list[str] = []
    by_source: dict[str, str] = {}
    for syntax in parse_image_syntax(text).images:
        source, alt = syntax.source, syntax.caption
        if source in by_source:
            continue
        try:
            path = _safe_asset_path(normalized_root, source)
            with path.open("rb") as stream:
                raw = stream.read(max_bytes + 1)
            image = validate_image(raw, max_bytes=max_bytes, max_pixels=max_pixels)
        except (ValueError, FileNotFoundError, OSError, ImageValidationError):
            unavailable.append(source)
            continue
        key = image.sha256
        by_source[source] = key
        metadata.setdefault(key, (source, image, raw, alt))

    for node in _tree_nodes(tree):
        node_text = sanitize_text(str(node.get("text") or ""))
        for syntax in parse_image_syntax(node_text, reference_markdown=text).images:
            key = by_source.get(syntax.source)
            if key is None:
                continue
            occurrence = ImageCatalogOccurrence(
                node_id=str(node["node_id"])
                if node.get("node_id") is not None
                else None,
                line_num=int(node.get("line_num") or 1)
                + node_text.count("\n", 0, syntax.start),
                char_offset=syntax.start,
                char_end=syntax.end,
            )
            occurrences.setdefault(key, []).append(occurrence)

    if not metadata:
        return ImageCatalogBuild(None, {})
    ordered: list[ImageCatalogEntry] = []
    raw_entries: list[dict[str, Any]] = []
    for key in sorted(metadata):
        source, image, _raw, alt = metadata[key]
        raw_entries.append(
            {
                "image_id": f"img_{key[:24]}",
                "source": source,
                "sha256": image.sha256,
                "media_type": image.media_type,
                "size_bytes": image.size_bytes,
                "width": image.width,
                "height": image.height,
                "orientation": image.orientation,
                "alt_text": alt,
                "occurrences": [item.model_dump() for item in occurrences.get(key, [])],
            }
        )
    digest = catalog_bundle_digest(raw_entries, tuple(unavailable))
    bundle_root = (
        Path("artifacts")
        / document_id
        / f"g{generation}"
        / "image_catalogs"
        / "v1"
        / digest
    )
    files: dict[str, bytes] = {}
    for item in raw_entries:
        source, image, raw, _alt = metadata[item["sha256"]]
        suffix = image.media_type.rsplit("/", 1)[1].replace("jpeg", "jpg")
        asset_relpath = str(bundle_root / "assets" / f"{image.sha256}.{suffix}")
        item["asset_relpath"] = asset_relpath
        files[asset_relpath] = raw
        ordered.append(ImageCatalogEntry(**item))
    catalog = ImageCatalog(
        document_id=document_id,
        generation=generation,
        bundle_digest=digest,
        entries=tuple(ordered),
        unavailable=tuple(sorted(set(unavailable))),
    )
    catalog_relpath = str(bundle_root / "images.json")
    files[catalog_relpath] = json.dumps(
        catalog.model_dump(exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    return ImageCatalogBuild(catalog, files)


def stage_image_catalog(workspace: Path, files: dict[str, bytes]) -> dict[str, bytes]:
    """Atomically publish a deterministic image bundle without overwriting it."""
    if not files:
        return {}
    catalog_paths = [path for path in files if path.endswith("/images.json")]
    if len(catalog_paths) != 1:
        raise ValueError("image catalog bundle must contain one images.json")
    target = (workspace / Path(catalog_paths[0]).parent).resolve()
    workspace_root = workspace.resolve()
    target.relative_to(workspace_root)
    for relpath in files:
        (workspace / relpath).resolve().relative_to(target)
    target.relative_to(workspace.resolve())
    if target.exists():
        for relpath, content in files.items():
            path = workspace / relpath
            if not path.is_file() or path.read_bytes() != content:
                raise ValueError(
                    "image catalog bundle already exists with different content"
                )
        return files
    staging = target.parent / f".staging-{uuid.uuid4().hex}"
    try:
        for relpath, content in files.items():
            relative = (workspace / relpath).resolve().relative_to(target)
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceArtifactStore.atomic_write(path, content)
        staging.replace(target)
    except Exception:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        raise
    return files


__all__ = [
    "ImageCatalog",
    "ImageCatalogBuild",
    "ImageCatalogEntry",
    "ImageCatalogOccurrence",
    "build_image_catalog",
    "stage_image_catalog",
]
