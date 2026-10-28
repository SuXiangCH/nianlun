"""Trusted child evidence enrichment and explicit parent re-read bridge."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from nianlun.agent.contracts import KnowledgeBasePort
from nianlun.agent.image_context import RequestImageContext
from nianlun.agent.lead_agent.runner import RetrievalCollector
from nianlun.agent.subagents.config import DeepSearchConfig
from nianlun.agent.subagents.result import (
    DeepSearchResult,
    Evidence,
    EvidenceImageRef,
    bound_result,
)


class ChildImageContext(RequestImageContext):
    """Read projection remains available; children never own answer selection."""

    def selection_preflight_error(self, call_id: str) -> str | None:
        return "Image selection is unavailable for child agents"


def enrich_image_evidence(
    result: DeepSearchResult, collector: RetrievalCollector, config: DeepSearchConfig
) -> DeepSearchResult:
    """Replace model identities with collector-owned evidence window metadata."""
    result = replace(
        result, evidence=tuple(replace(item, image_refs=()) for item in result.evidence)
    )
    if not collector.image_context.enabled or result.status != "completed":
        return bound_result(result, config)
    evidence: list[Evidence] = []
    for item in result.evidence[: config.max_evidence_items]:
        matched = next(
            (
                snippet
                for snippet in collector.snippets
                if item.doc_id == snippet.get("doc_id")
                and item.node_id == snippet.get("node_id")
                and item.char_offset == snippet.get("char_offset")
                and item.text == str(snippet.get("model_text", ""))[:600]
            ),
            None,
        )
        if matched is None:
            evidence.append(replace(item, image_refs=()))
            continue
        refs = []
        projected = {
            (image["ref_id"], image["source_start"], image["source_end"]): image
            for image in matched.get("model_images", [])
        }
        for image in matched.get("images", [])[: config.max_evidence_image_refs]:
            model_image = projected.get(
                (image["ref_id"], image["source_start"], image["source_end"])
            )
            if model_image is None:
                continue
            try:
                refs.append(
                    EvidenceImageRef.model_validate(
                        {
                            **{
                                name: image[name]
                                for name in (
                                    "ref_id",
                                    "knowledge_base_id",
                                    "generation",
                                    "content_version",
                                    "image_id",
                                    "sha256",
                                    "source_start",
                                    "source_end",
                                )
                            },
                            "text_start": model_image["text_start"],
                            "text_end": model_image["text_end"],
                        }
                    )
                )
            except ValidationError:
                continue
        evidence.append(
            replace(
                Evidence.from_mapping(matched),
                text=matched["model_text"],
                # The bridge re-reads exactly the observed source window,
                # independent of projected text length or caller request size.
                char_limit=len(matched["text"]),
                image_refs=tuple(refs),
            )
        )
    return bound_result(replace(result, evidence=tuple(evidence)), config)


def reread_image_refs(
    result: DeepSearchResult,
    *,
    knowledge_base: KnowledgeBasePort,
    collector: RetrievalCollector,
    config: DeepSearchConfig,
) -> dict[str, str]:
    """Revalidate source identity before allocating any parent-local ref."""
    if result.status != "completed" or not collector.image_context.enabled:
        return {}
    result = bound_result(result, config)
    remapped: dict[str, str] = {}
    for evidence in result.evidence:
        if (
            not evidence.image_refs
            or not evidence.doc_id
            or not evidence.line_spec
            or evidence.char_offset is None
            or evidence.char_offset < 0
            or evidence.char_limit is None
            or not 0 < evidence.char_limit <= 8000
        ):
            continue
        raw = knowledge_base.get_line_content(
            evidence.doc_id,
            evidence.line_spec,
            char_offset=evidence.char_offset,
            char_limit=evidence.char_limit,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("doc_id") != evidence.doc_id:
            continue
        verified_items = []
        local_matches: list[tuple[EvidenceImageRef, dict[str, Any]]] = []
        for item in payload.get("content", []):
            if (
                not isinstance(item, dict)
                or item.get("node_id") != evidence.node_id
                or item.get("char_offset") != evidence.char_offset
                or len(str(item.get("text", ""))) != evidence.char_limit
            ):
                continue
            probe = RetrievalCollector(
                tool_schema_version=5,
                image_capability=True,
                image_policy=collector.image_policy,
            )
            source_payload = {**payload, "content": [item]}
            probe.add_line_content_result(json.dumps(source_payload))
            approved = []
            for image in probe.image_context.candidates.values():
                conflict = any(
                    all(
                        current.get(name) == image.get(name)
                        for name in (
                            "knowledge_base_id",
                            "doc_id",
                            "generation",
                            "image_id",
                        )
                    )
                    and any(
                        current.get(name) != image.get(name)
                        for name in ("content_version", "sha256")
                    )
                    for current in collector.image_context.candidates.values()
                )
                if conflict:
                    continue
                for ref in evidence.image_refs:
                    if all(
                        image.get(name) == getattr(ref, name)
                        for name in (
                            "knowledge_base_id",
                            "generation",
                            "content_version",
                            "image_id",
                            "sha256",
                            "source_start",
                            "source_end",
                        )
                    ):
                        approved.append(image)
                        local_matches.append((ref, image))
            if approved:
                verified_items.append({**item, "images": approved})
        if not verified_items:
            continue
        # Only verified image identities enter the parent's normal projection.
        collector.add_line_content_result(
            json.dumps({**payload, "content": verified_items})
        )
        for ref, image in local_matches:
            for parent_ref, candidate in collector.image_context.candidates.items():
                if all(
                    candidate.get(name) == image.get(name)
                    for name in (
                        "knowledge_base_id",
                        "doc_id",
                        "generation",
                        "content_version",
                        "image_id",
                        "sha256",
                    )
                ):
                    remapped[ref.ref_id] = parent_ref
                    break
    return remapped
