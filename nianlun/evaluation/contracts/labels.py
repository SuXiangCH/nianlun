"""Versioned, display-only bilingual enum annotations."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import AttributionCategory

ATTRIBUTION_LABEL_VERSION = "2026-08-19.v1"


class LocalizedEnumAnnotation(EvaluationSchema):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label_zh: str = Field(min_length=1)
    label_en: str = Field(min_length=1)
    description_zh: str = Field(min_length=1)
    description_en: str = Field(min_length=1)


ATTRIBUTION_ANNOTATIONS: dict[AttributionCategory, LocalizedEnumAnnotation] = {
    AttributionCategory.RETRIEVAL_MISSING: LocalizedEnumAnnotation(
        label_zh="检索缺失",
        label_en="Missing retrieval evidence",
        description_zh="没有检索结果，或检索内容完全不包含回答所需事实。",
        description_en="No retrieved context contains the evidence required to answer.",
    ),
    AttributionCategory.RETRIEVAL_INCOMPLETE: LocalizedEnumAnnotation(
        label_zh="检索不完整",
        label_en="Incomplete retrieval evidence",
        description_zh="检索结果只覆盖回答问题所需的部分关键事实。",
        description_en="Retrieved contexts cover only part of the required facts.",
    ),
    AttributionCategory.RETRIEVAL_NOISE: LocalizedEnumAnnotation(
        label_zh="检索噪声",
        label_en="Retrieval noise",
        description_zh="错误内容与无关或冲突的上下文存在直接对应。",
        description_en="The error is directly linked to irrelevant or conflicting context.",
    ),
    AttributionCategory.GENERATION_EMPTY: LocalizedEnumAnnotation(
        label_zh="空回答",
        label_en="Empty generation",
        description_zh="实际回答为空字符串或纯空白。",
        description_en="The actual answer is empty or contains only whitespace.",
    ),
    AttributionCategory.GENERATION_INCOMPLETE: LocalizedEnumAnnotation(
        label_zh="回答不完整",
        label_en="Incomplete generation",
        description_zh="检索证据充分，但回答遗漏了已有关键事实。",
        description_en="Evidence is sufficient, but the answer omits available key facts.",
    ),
    AttributionCategory.HALLUCINATION: LocalizedEnumAnnotation(
        label_zh="幻觉",
        label_en="Hallucination",
        description_zh="最终错误回答包含无依据或被证据否定的事实断言。",
        description_en="The incorrect answer contains unsupported or contradicted claims.",
    ),
    AttributionCategory.REASONING_ERROR: LocalizedEnumAnnotation(
        label_zh="推理错误",
        label_en="Reasoning error",
        description_zh="所需证据充分，但计算、比较、归纳或时序推理错误。",
        description_en="Evidence is sufficient, but the answer applies faulty reasoning.",
    ),
    AttributionCategory.UNKNOWN: LocalizedEnumAnnotation(
        label_zh="无法归因",
        label_en="Unknown attribution",
        description_zh="现有输入不足以可靠区分错误归因。",
        description_en="The available inputs are insufficient for reliable attribution.",
    ),
}
