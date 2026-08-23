"""Prompt for constrained error attribution."""

from __future__ import annotations

from collections.abc import Sequence

from nianlun.evaluation.contracts.case import EvaluationCase
from nianlun.evaluation.contracts.enums import AttributionCategory
from nianlun.evaluation.stages.common import (
    json_block,
    untrusted_input_notice,
)
from nianlun.evaluation.stages.critic.schema import CriticResult
from nianlun.evaluation.stages.evidence.schema import EvidenceResult

PROMPT_VERSION = "2026-08-21.v2"

_CATEGORY_GUIDANCE: dict[AttributionCategory, str] = {
    AttributionCategory.RETRIEVAL_MISSING: "no answer-essential evidence was retrieved",
    AttributionCategory.RETRIEVAL_INCOMPLETE: "some required evidence was absent, and that absence plausibly caused the defective answer",
    AttributionCategory.RETRIEVAL_NOISE: "specific irrelevant, misleading, or conflicting contexts directly influenced the defective answer",
    AttributionCategory.GENERATION_EMPTY: "the actual answer contained no substantive content",
    AttributionCategory.HALLUCINATION: "the answer introduced a factual claim unsupported by available evidence or contradicted by evidence",
    AttributionCategory.GENERATION_INCOMPLETE: "sufficient evidence was available, but the answer omitted an answer-essential fact",
    AttributionCategory.REASONING_ERROR: "sufficient facts were available, but the answer made an invalid calculation, comparison, inference, or temporal step",
    AttributionCategory.UNKNOWN: "the supplied information cannot reliably distinguish the cause",
}


def build_prompt(
    case: EvaluationCase,
    evidence: EvidenceResult,
    critic: CriticResult,
    allowed_attributions: Sequence[AttributionCategory],
) -> str:
    payload = {
        "case": case.model_dump(mode="json"),
        "evidence_result": evidence.model_dump(mode="json"),
        "final_critic_result": critic.model_dump(mode="json"),
        "allowed_attributions": [item.value for item in allowed_attributions],
    }
    category_guidance = "\n".join(
        f"- {item.value}: {_CATEGORY_GUIDANCE[item]}." for item in allowed_attributions
    )
    return f"""You are an error attribution analyst. The final answer verdict is already fixed;
identify the cause best supported by the supplied evidence without changing that verdict.

Allowed category definitions:
{category_guidance}

Attribution procedure:
1. Identify the concrete defect in the actual answer.
2. Determine whether the information needed to avoid that defect was present in the contexts.
3. Select the most direct cause as value, not merely a correlated observation.
4. Add a secondary issue only when it is independently evidenced and materially contributed.

Boundary and precedence rules:
- Select value and every secondary_issues item only from allowed_attributions.
- Distinguish retrieval_incomplete from generation_incomplete by whether the omitted fact was
  available in the contexts.
- Use reasoning_error when the required facts were available and the defect is a traceable bad
  inference or calculation. Use hallucination for an introduced factual assertion that cannot be
  derived from available evidence or is contradicted by it.
- Use retrieval_noise only when specific noise contexts can be cited and linked to the defect.
- Use unknown rather than forcing a category when multiple causes remain indistinguishable.

Strength rules:
- strong: direct, specific evidence leaves no material competing explanation.
- plausible: evidence favors the attribution but a meaningful alternative remains.
- insufficient: evidence cannot support a reliable attribution; this requires unknown.

Evidence-field rules:
- value is the one primary cause. secondary_issues contains only independently evidenced causes
  that materially contributed; it must not repeat value.
- attribution_strength describes confidence in value only, not in secondary_issues.
- unknown requires attribution_strength=insufficient and cannot be a secondary issue.
- A hallucination issue requires non-empty hallucinated_claims. unsupported means the supplied
  material provides no support and is not proof that the claim is false; it cannot cite
  contradictory evidence. contradicted requires at least one valid contradictory context_id.
  contradicted_by_reference only records an additional conflict with the reference answer; it
  cannot establish contradiction by itself.
- Populate omitted_facts for material omissions, reasoning_errors for traceable reasoning defects,
  and noise_context_ids only for noise contexts directly linked to the defect. Leave a list empty
  when no corresponding confirmed item exists.
- All context IDs must come from the input retrieval_contexts.
- Write reason, claim text, omitted facts, and reasoning errors in the language of the question
  when practical.
{untrusted_input_notice()}

<evaluation_input>
{json_block(payload)}
</evaluation_input>"""
