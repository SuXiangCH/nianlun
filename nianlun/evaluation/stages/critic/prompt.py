"""Branch-specific corrective prompt for the critic stage."""

from __future__ import annotations

from nianlun.evaluation.contracts.case import EvaluationCase
from nianlun.evaluation.contracts.enums import CriticPromptId
from nianlun.evaluation.stages.common import (
    json_block,
    untrusted_input_notice,
)
from nianlun.evaluation.stages.correctness.schema import CorrectnessResult
from nianlun.evaluation.stages.evidence.schema import EvidenceResult
from nianlun.evaluation.stages.critic.routing import CriticRoute

COMMON_PROMPT_VERSION = "2026-08-22.v3"

_BRANCH_INSTRUCTIONS: dict[CriticPromptId, str] = {
    CriticPromptId.REFERENCE_CHALLENGE: """Additional review focus for this case: reference challenge
1. Identify the exact reference defect: missing core information, internal conflict, or conflict
   with the supplied evidence.
2. Re-evaluate the actual answer against the question and evidence without treating reference
   absence as proof of error.
3. Do not automatically favor the actual answer: require positive support for material claims.
4. Return uncertain when neither the reference nor the evidence can settle a core requirement.""",
    CriticPromptId.FALSE_NEGATIVE_RECOVERY: """Additional review focus for this case: false-negative recovery
1. Look for semantic equivalence, concise but sufficient answers, and compatible facts omitted
   from the reference but supported by evidence.
2. Verify every core requirement before recovering the answer to correct.
3. Check for material unsupported or contradictory additions that still require a partial or
   incorrect verdict.
4. Overturn only when the recovered verdict is supported by concrete facts, not merely because
   the evidence-review aggregate says full.""",
    CriticPromptId.FALSE_POSITIVE_CORRECTION: """Additional review focus for this case: false-positive correction
1. Identify which material claims lack support or are contradicted and whether they affect the
   central answer.
2. Distinguish absent context support from affirmative evidence that the answer is wrong.
3. Check whether surface similarity to the reference concealed a core error or omission.
4. Preserve correct when the answer remains semantically correct and the evidence gap only shows
   that the supplied contexts are incomplete.""",
    CriticPromptId.EVIDENCE_CONFLICT_RESOLUTION: """Additional review focus for this case: evidence conflict
1. Locate each material conflict and determine whether it concerns a core or optional fact.
2. Check whether source content, specificity, or the question resolves the conflict without
   inventing missing information.
3. Do not average incompatible claims or select one solely because it matches the reference.
4. Return uncertain when an unresolved conflict can change the central verdict.""",
    CriticPromptId.SEVERITY_BOUNDARY_CORRECTION: """Additional review focus for this case: severity boundary
1. Separate the central conclusion from secondary requirements and optional details.
2. Use partially_correct only when the central answer remains useful and substantially correct
   despite a material local omission or error.
3. Use incorrect when the main request is unmet, the central conclusion is wrong, or the defect
   makes the answer materially misleading.
4. Do not downgrade correct for style or optional detail, and do not soften a core error to
   partially_correct.""",
    CriticPromptId.GENERAL: """Additional review focus for this case: general review
1. Attempt to falsify the preliminary verdict by checking every core question requirement.
2. Verify that the evidence observations and cited contexts support their material conclusions.
3. Check for overlooked semantic equivalence, omissions, contradictions, and unsupported claims.
4. Confirm the preliminary verdict only when no material counterexample remains; otherwise
   overturn it or return uncertain.""",
}


def build_prompt(
    case: EvaluationCase,
    preliminary: CorrectnessResult,
    evidence: EvidenceResult,
    route: CriticRoute,
) -> str:
    payload = {
        "case": case.model_dump(mode="json"),
        "preliminary_correctness": preliminary.model_dump(mode="json"),
        "evidence_result": evidence.model_dump(mode="json"),
        "routing_flags": [flag.value for flag in route.routing_flags],
    }
    return f"""You are the final answer-quality reviewer. Re-evaluate the answer using the original
case, the preliminary correctness judgment, and the evidence observations.

Evidence hierarchy and independence:
- The question defines the requirements.
- The reference answer is an important comparison basis, but its assessed quality matters.
- The supplied contexts can support or contradict material claims; absence of context support is
  not by itself proof that a claim is false.
- The preliminary judgment is a hypothesis, not an authoritative conclusion.
- Verify material upstream claims against the original input before relying on them.

Final verdict boundaries:
- correct: all core requirements are satisfied with no material factual or logical error.
- partially_correct: the central answer is substantially correct and useful, but contains a
  material secondary omission or localized error.
- incorrect: the central conclusion is wrong, the main request is unmet, or a defect makes the
  answer materially misleading.
- uncertain: supplied information cannot resolve a material ambiguity or conflict. Do not use it
  merely because review is difficult.

Answer-completeness and evidence-conflict rules:
- Judge answer completeness only from the requirements explicitly stated or necessarily implied
  by the question.
- Do not treat unrequested caveats, evidence discussion, source-consistency analysis, exclusion
  reasoning, or background information as missing answer content.
- When the actual answer satisfies the question, matches the answer-essential content of the
  reference, and has direct supporting evidence, preserve correct unless the answer contains an
  additional material error or the supplied evidence directly establishes that a core claim is
  false.
- An evidence inconsistency may change the answer verdict only when it concerns a required core
  claim, is genuinely irreconcilable under the same scope and conditions, and materially prevents
  determining whether that claim is true.
- Do not convert a defect captured by evidence_consistency or reference_quality into an answer
  error unless it directly invalidates a required core claim.

You may confirm or overturn the preliminary verdict. Judge answer quality only; do not perform
error attribution. Return a complete final correctness assessment: its reason and matched_facts,
missing_facts, and incorrect_claims must all support the final verdict, even when you confirm the
preliminary verdict. Reassess reference_quality rather than copying it, and give it a separate
reason about the quality of the reference itself. Write reasons and factual list items in the
language of the question when practical.
Do not emit routing metadata or fields outside the requested schema.

Follow these checks in addition to the common rules:

{_BRANCH_INSTRUCTIONS[route.prompt_id]}
{untrusted_input_notice()}

<evaluation_input>
{json_block(payload)}
</evaluation_input>"""
