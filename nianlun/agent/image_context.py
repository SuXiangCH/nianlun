"""Request-local image identities and bounded model-safe source projections."""

from __future__ import annotations

import logging
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nianlun.agent.answer_images import escape_image_caption
from nianlun.agent.contracts import AnswerImagePolicy

logger = logging.getLogger(__name__)


class SourceImage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    image_id: str = Field(pattern=r"^img_[0-9a-f]{16,64}$")
    generation: int = Field(ge=1)
    knowledge_base_id: str = Field(min_length=1)
    content_version: int = Field(ge=0)
    asset_relpath: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    orientation: int = Field(default=1, ge=1, le=8)
    alt_text: str = ""
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class ImageSelectionPlan:
    allowed_refs: frozenset[str]
    error: str | None = None


class RequestImageContext:
    def __init__(self, *, enabled: bool, policy: AnswerImagePolicy) -> None:
        self.enabled = enabled
        self.policy = policy
        self._lock = threading.RLock()
        self._identity_refs: dict[tuple[str, str, int, str], str] = {}
        self._candidates: dict[str, dict[str, Any]] = {}
        self.truncated = False
        self._selection_plans: dict[str, ImageSelectionPlan] = {}
        self._selected_refs: tuple[str, ...] = ()

    @property
    def candidates(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._candidates)

    @property
    def selected_images(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(self._candidates[ref]) for ref in self._selected_refs]

    def freeze_tool_batch(self, calls: list[dict[str, Any]]) -> None:
        selection = [
            call for call in calls if call.get("name") == "select_answer_images"
        ]
        with self._lock:
            allowed = frozenset(self._candidates)
            error = "每个工具批次只允许一次图片选择" if len(selection) > 1 else None
            self._selection_plans = {
                call["id"]: ImageSelectionPlan(allowed, error)
                for call in selection[:128]
                if isinstance(call.get("id"), str)
            }

    def selection_preflight_error(self, call_id: str) -> str | None:
        with self._lock:
            if not self.enabled:
                return "当前请求未启用图片输出"
            plan = self._selection_plans.get(call_id)
            if plan is None:
                return "图片选择缺少工具批次预检"
            return plan.error

    def select_answer_images(self, call_id: str, refs: list[str]) -> None:
        with self._lock:
            self.validate_answer_images(call_id, refs)
            self._selected_refs = tuple(refs)

    def validate_answer_images(self, call_id: str, refs: list[str]) -> None:
        """Check a frozen plan without changing selection before tool execution."""
        with self._lock:
            error = self.selection_preflight_error(call_id)
            if error:
                raise ValueError(error)
            if (
                not refs
                or len(refs) > self.policy.max_candidates
                or len(refs) > self.policy.max_occurrences
                or len(refs) != len(set(refs))
            ):
                raise ValueError("图片编号列表必须非空、有界且不重复")
            plan = self._selection_plans[call_id]
            if any(ref not in plan.allowed_refs for ref in refs):
                raise ValueError("只能选择本工具批次开始前已读取的图片编号")
            unique = {
                (
                    self._candidates[ref]["knowledge_base_id"],
                    self._candidates[ref]["sha256"],
                ): self._candidates[ref]
                for ref in refs
            }
            total = 0
            for count, image in enumerate(unique.values()):
                if self.policy.archive_budget_reason(
                    image["size_bytes"], image["width"] * image["height"], count, total
                ):
                    raise ValueError(
                        "所选图片超出本次请求的图片资源预算，请减少图片数量"
                    )
                total += image["size_bytes"]

    def clear(self) -> None:
        with self._lock:
            self._identity_refs.clear()
            self._candidates.clear()
            self.truncated = False
            self._selection_plans.clear()
            self._selected_refs = ()

    def _register(
        self, raw: dict[str, Any], doc_id: str, item: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            image = SourceImage.model_validate(raw)
        except ValidationError:
            logger.warning("agent.image_reference_rejected reason=invalid_metadata")
            return None
        identity = (image.knowledge_base_id, doc_id, image.generation, image.image_id)
        with self._lock:
            ref = self._identity_refs.get(identity)
            if ref is None:
                if len(self._candidates) >= self.policy.max_candidates:
                    self.truncated = True
                    return None
                ref = f"img_{len(self._candidates) + 1:02d}"
                self._identity_refs[identity] = ref
                self._candidates[ref] = {
                    **image.model_dump(),
                    "doc_id": doc_id,
                    "ref_id": ref,
                    "source_bindings": [],
                }
            binding = {
                field: self._candidates[ref][field]
                for field in (
                    "knowledge_base_id",
                    "doc_id",
                    "generation",
                    "content_version",
                    "image_id",
                )
            }
            binding.update(
                {
                    "citation_id": item["citation_id"],
                    "node_id": item.get("node_id"),
                    "char_offset": item.get("char_offset"),
                    "char_limit": item.get("char_limit"),
                    "source_start": image.source_start,
                    "source_end": image.source_end,
                }
            )
            if binding not in self._candidates[ref]["source_bindings"]:
                self._candidates[ref]["source_bindings"].append(binding)
            return {**image.model_dump(), "doc_id": doc_id, "ref_id": ref}

    def project_item(
        self, item: dict[str, Any], doc_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        model = {
            field: item[field]
            for field in (
                "node_id",
                "title",
                "line_num",
                "text",
                "char_offset",
                "char_limit",
                "total_chars",
                "text_truncated",
                "next_char_offset",
                "citation_id",
            )
            if field in item
        }
        model["offset_basis"] = "source_node"
        source_text = str(item.get("text") or "")
        start = int(item.get("char_offset") or 0)
        end = start + len(source_text)
        raw_images = item.get("images") or []
        by_range = {
            (image.get("source_start"), image.get("source_end")): image
            for image in raw_images
            if isinstance(image, dict)
        }
        spans = item.get("image_spans") or []
        parts: list[str] = []
        projected_images: list[dict[str, Any]] = []
        registered: list[dict[str, Any]] = []
        cursor = 0
        projected_length = 0
        for span in sorted(spans, key=lambda span: span["source_start"]):
            first, last = span["source_start"], span["source_end"]
            if first >= end or last <= start:
                continue
            local_start, local_end = max(first, start) - start, min(last, end) - start
            if local_start < cursor:
                continue
            prefix = source_text[cursor:local_start]
            parts.append(prefix)
            projected_length += len(prefix)
            raw = by_range.get((first, last))
            image = (
                self._register(raw, doc_id, item)
                if (
                    raw is not None
                    and first >= start
                    and last <= end
                    and span.get("kind") == "markdown"
                )
                else None
            )
            if image is None:
                replacement = "[图片引用不可用]"
            else:
                replacement = f"![{escape_image_caption(image['alt_text'])}](nianlun-image:{image['ref_id']})"
                registered.append(image)
                projected_images.append(
                    {
                        "ref_id": image["ref_id"],
                        "caption": image["alt_text"][:256],
                        "width": image["width"],
                        "height": image["height"],
                        "status": "available",
                        "citation_ids": [item["citation_id"]],
                        "source_start": first,
                        "source_end": last,
                        "text_start": projected_length,
                        "text_end": projected_length + len(replacement),
                    }
                )
            parts.append(replacement)
            projected_length += len(replacement)
            cursor = local_end
        parts.append(source_text[cursor:])
        model["text"] = "".join(parts)
        if self.enabled:
            model["images"] = projected_images
            model["image_candidates_truncated"] = self.truncated
            model["image_candidates_total"] = len(by_range)
            model["image_budget"] = self.policy.model_message()["content"]
        return model, registered


__all__ = ["AnswerImagePolicy", "RequestImageContext", "SourceImage"]
