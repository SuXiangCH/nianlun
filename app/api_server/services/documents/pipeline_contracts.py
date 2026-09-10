"""Validated payloads persisted between document pipeline stages."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_NORMALIZATION_CHUNKS = 100


class NormalizationChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    markdown_relpath: str = Field(min_length=1)
    output_sha256: str = Field(min_length=64, max_length=64)
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_page_range(self) -> Self:
        if (self.source_page_start is None) != (self.source_page_end is None):
            raise ValueError("分段页码范围必须同时提供起止页")
        if (
            self.source_page_start is not None
            and self.source_page_end is not None
            and self.source_page_start > self.source_page_end
        ):
            raise ValueError("分段起始页不能大于结束页")
        return self


class NormalizationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    pipeline_generation: int = Field(ge=1)
    chunks: list[NormalizationChunk] = Field(
        min_length=1, max_length=MAX_NORMALIZATION_CHUNKS
    )

    @model_validator(mode="after")
    def validate_chunks(self) -> Self:
        ordered = sorted(self.chunks, key=lambda item: item.chunk_index)
        if [item.chunk_index for item in ordered] != list(range(len(ordered))):
            raise ValueError("分段编号必须从 0 开始连续")
        previous_end: int | None = None
        for item in ordered:
            if item.source_page_start is None:
                continue
            if previous_end is not None and item.source_page_start <= previous_end:
                raise ValueError("分段页码范围不能重叠或逆序")
            previous_end = item.source_page_end
        self.chunks = ordered
        return self


class PageMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown_line_start: int = Field(ge=1)
    markdown_line_end: int = Field(ge=1)
    chunk_index: int = Field(ge=0)
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    evidence: Literal["content_list", "chunk_boundary"]


class PageMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    precision: Literal["exact", "chunk", "unavailable"]
    entries: list[PageMapEntry]


class EnrichmentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_enabled: bool
    heading_recovery_enabled: bool
    subtree_folding_enabled: bool
    min_subtree_tokens: int = Field(ge=1)


__all__ = [
    "EnrichmentOptions",
    "MAX_NORMALIZATION_CHUNKS",
    "NormalizationChunk",
    "NormalizationInput",
    "PageMap",
    "PageMapEntry",
]
