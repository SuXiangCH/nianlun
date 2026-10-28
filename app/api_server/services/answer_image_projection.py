"""Deterministic public Markdown projection; model output never supplies URLs."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api_server.apis.v1.runtime_schemas import PublicAnswerImage, PublicChatResponse
from app.api_server.common.errors import ApiError, success
from nianlun.agent.answer_images import normalize_answer_images, escape_image_caption
from nianlun.agent.contracts import AnswerImagePolicy


class InlineImageBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    blob_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(gt=0)


class InlineProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    storage_format: Literal["inline_projection_v1"] = "inline_projection_v1"
    response: PublicChatResponse
    bindings: list[InlineImageBinding] = Field(default_factory=list)
    response_max_bytes: int = Field(gt=0)
    policy: AnswerImagePolicy = AnswerImagePolicy()


ImageReader = Callable[[dict[str, Any]], bytes | None]


class AnswerImageProjection:
    def __init__(
        self,
        *,
        inline_max_bytes: int = 1024 * 1024,
        inline_total_bytes: int = 2 * 1024 * 1024,
        inline_response_max_bytes: int = 8 * 1024 * 1024,
        policy: AnswerImagePolicy = AnswerImagePolicy(),
    ) -> None:
        if min(inline_max_bytes, inline_total_bytes, inline_response_max_bytes) < 1:
            raise ValueError("内联图片限额必须大于 0")
        self.inline_max_bytes = inline_max_bytes
        self.inline_total_bytes = inline_total_bytes
        self.inline_response_max_bytes = inline_response_max_bytes
        self.policy = policy

    @staticmethod
    def _image(item: dict[str, Any]) -> PublicAnswerImage:
        return PublicAnswerImage(
            id=item["id"],
            ref_ids=item.get("ref_ids", []),
            citation_ids=list(
                dict.fromkeys(
                    binding["citation_id"]
                    for binding in item.get("source_bindings", [])
                    if isinstance(binding.get("citation_id"), int)
                )
            ),
            caption=item.get("caption", ""),
            width=item.get("width"),
            height=item.get("height"),
            orientation=item.get("orientation", 1),
            status=item["status"],
            unavailable_reason=item.get("unavailable_reason"),
        )

    def prepare(
        self,
        response: PublicChatResponse,
        images: list[dict[str, Any]],
        *,
        transport: Literal["url", "data_url"],
        read_bytes: ImageReader,
        url_for: Callable[[dict[str, Any]], tuple[str, str]] | None = None,
    ) -> tuple[PublicChatResponse, dict[str, Any]]:
        projected: list[PublicAnswerImage] = []
        bindings: list[InlineImageBinding] = []
        total = 0
        for item in images:
            if not item.get("display_in_answer", True):
                continue
            image = self._image(item)
            projected.append(image)
            if image.status != "available":
                continue
            if transport == "url":
                if url_for is None:
                    raise ValueError("公开图片地址未配置")
                image.url, image.expires_at = url_for(item)
                continue
            size = int(item["size_bytes"])
            if size > self.inline_max_bytes or total + size > self.inline_total_bytes:
                image.status, image.unavailable_reason = (
                    "unavailable",
                    "inline_budget_exceeded",
                )
                continue
            raw = read_bytes(item)
            if raw is None:
                image.status, image.unavailable_reason = (
                    "unavailable",
                    "image_unavailable",
                )
                continue
            binding = InlineImageBinding(
                id=image.id,
                blob_id=item["blob_id"],
                sha256=item["sha256"],
                media_type=item["media_type"],
                size_bytes=size,
            )
            self._check_binding(raw, binding)
            image.url = self._data_url(raw, binding.media_type)
            bindings.append(binding)
            total += size
        base = response.model_copy(
            update={"images": projected, "image_transport": transport}, deep=True
        )
        result = self.render(base)
        if transport == "url":
            return result, result.model_dump(mode="json")
        self._check_size(result, self.inline_response_max_bytes)
        # Freeze failures and positions, not encoded bytes or a bearer grant.
        for image in base.images:
            image.url = None
            image.expires_at = None
        stored = InlineProjection(
            response=base,
            bindings=bindings,
            response_max_bytes=self.inline_response_max_bytes,
            policy=self.policy,
        )
        return result, stored.model_dump(mode="json")

    def replay_inline(
        self,
        plan: InlineProjection,
        *,
        read_bytes: ImageReader,
    ) -> PublicChatResponse:
        response = plan.response.model_copy(deep=True)
        bindings = {item.id: item for item in plan.bindings}
        for image in response.images:
            if image.status != "available":
                continue
            binding = bindings.get(image.id)
            if binding is None:
                raise ValueError("内联图片投影缺少绑定")
            raw = read_bytes(binding.model_dump())
            if raw is None:
                image.status, image.unavailable_reason = (
                    "unavailable",
                    "image_unavailable",
                )
                image.url = None
                continue
            self._check_binding(raw, binding)
            image.url = self._data_url(raw, binding.media_type)
        result = self.render(response, policy=plan.policy)
        self._check_size(result, plan.response_max_bytes)
        return result

    @staticmethod
    def _check_binding(raw: bytes, binding: InlineImageBinding) -> None:
        if len(raw) != binding.size_bytes:
            raise ValueError("图片字节与元数据不一致")
        if hashlib.sha256(raw).hexdigest() != binding.sha256:
            raise ValueError("图片摘要与冻结绑定不一致")

    @staticmethod
    def _data_url(raw: bytes, media_type: str) -> str:
        return f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"

    @staticmethod
    def _check_size(result: PublicChatResponse, maximum: int) -> None:
        encoded = json.dumps(
            success(result.model_dump(mode="json")), ensure_ascii=False
        ).encode("utf-8")
        if len(encoded) > maximum:
            raise ApiError("图片响应大小超出限制", 413)

    def render(
        self, response: PublicChatResponse, *, policy: AnswerImagePolicy | None = None
    ) -> PublicChatResponse:
        policy = policy or self.policy
        images = {item.id: item for item in response.images}
        answer = response.answer
        if response.answer_output_mode == "images" and not answer:
            answer = "\n\n".join(
                f"![](nianlun-image:{image.id})" for image in response.images
            )
        normalized, occurrences = normalize_answer_images(
            answer,
            {image.id: (image.id, image.caption) for image in response.images},
            max_occurrences=policy.max_occurrences,
            max_candidate_line_bytes=policy.max_candidate_line_bytes,
            max_canonical_bytes=policy.max_canonical_bytes,
        )
        parts: list[str] = []
        cursor = 0
        for occurrence in occurrences:
            parts.append(normalized[cursor : occurrence["start"]])
            image = images[occurrence["attachment_id"]]
            if image.status != "available" or not image.url:
                parts.append("图片暂不可用")
            elif occurrence["kind"] == "repeat":
                parts.append(
                    "见前图"
                    + "".join(f" [{citation}]" for citation in image.citation_ids)
                )
            else:
                parts.append(f"![{escape_image_caption(image.caption)}]({image.url})")
            cursor = occurrence["end"]
        parts.append(normalized[cursor:])
        mode = response.answer_output_mode
        if not any(
            image.status == "available" and image.url for image in response.images
        ):
            mode = "text"
        elif mode != "images":
            mode = "text_and_images"
        return response.model_copy(
            update={"answer": "".join(parts), "answer_output_mode": mode}, deep=True
        )


def reconcile_completion_projection(
    stored: dict[str, Any],
    raw: dict[str, Any],
    *,
    policy: AnswerImagePolicy = AnswerImagePolicy(),
) -> dict[str, Any]:
    """Reconcile a frozen projection with transaction-validated source bindings.

    This is pure: it neither signs new grants nor reads bytes. Inline results
    retain their descriptor and must be rebuilt after the transaction commits.
    """
    inline = stored.get("storage_format") == "inline_projection_v1"
    plan = InlineProjection.model_validate(stored) if inline else None
    response = (
        plan.response.model_copy(deep=True)
        if plan is not None
        else PublicChatResponse.model_validate(stored)
    )
    current = {image["id"]: image for image in raw.get("images", [])}
    for image in response.images:
        item = current.get(image.id)
        if item is None:
            raise ValueError("公开图片投影缺少完成记录")
        image.citation_ids = list(
            dict.fromkeys(
                binding["citation_id"]
                for binding in item.get("source_bindings", [])
                if isinstance(binding.get("citation_id"), int)
            )
        )
        if item["status"] != "available":
            image.status = item["status"]
            image.unavailable_reason = item.get("unavailable_reason")
            image.url = image.expires_at = None
    response.answer = raw["answer"]
    response.answer_output_mode = raw.get("answer_output_mode", "text")
    if plan is not None:
        available = {
            image.id for image in response.images if image.status == "available"
        }
        return plan.model_copy(
            update={
                "response": response,
                "bindings": [
                    binding for binding in plan.bindings if binding.id in available
                ],
            }
        ).model_dump(mode="json")
    return AnswerImageProjection(policy=policy).render(response).model_dump(mode="json")


__all__ = [
    "AnswerImageProjection",
    "InlineProjection",
    "reconcile_completion_projection",
]
