"""Prompt for the independent retrieval evidence reviewer."""

from __future__ import annotations

from nianlun.evaluation.contracts.case import EvaluationCase
from nianlun.evaluation.stages.common import (
    json_block,
    untrusted_input_notice,
)

PROMPT_VERSION = "2026-08-22.v7"


def build_prompt(case: EvaluationCase) -> str:
    payload = case.model_dump(mode="json")
    empty_retrieval_rule = (
        "The retrieval_contexts list is empty. You must return retrieval_coverage.value=none, "
        "retrieval_noise.value=none, and empty context_ids lists for every metric and claim."
        if not case.retrieval_contexts
        else "Every cited context_id must exactly match an ID in retrieval_contexts."
    )
    return f"""You are an evidence analyst responsible for describing what the supplied contexts
do and do not support.

Use only the supplied contexts as evidence. Use the question and answers to identify required
information and material claims, but do not treat agreement between the two answers as evidence.
Do not decide whether the actual answer is correct and do not diagnose the cause of an error.
Assess the reference answer and actual answer independently.

Observation boundaries:
- Retrieval coverage describes whether the contexts contain the answer-essential information
  required by the question: none means no required fact is available; partial means only some
  required facts are available; full means all required facts are available; uncertain means the
  supplied material does not permit a stable coverage assessment.
- Retrieval noise describes irrelevant or distracting material: none means no material noise;
  limited means some noise exists but relevant evidence remains easy to identify; substantial
  noise means distracting, misleading, or irrelevant material could materially impede use.
- Evidence consistency is conflicting only when contexts make materially incompatible claims
  about an answer-essential fact. Different wording or complementary detail is not a conflict.
- Support is evaluated separately for each claim. For one claim, full means its material content is
  completely supported, partial means only part of that claim is supported, none means no support,
  conflicting means the claim is contradicted, and uncertain applies when the evidence is ambiguous.
  No support means absence of support, not contradiction. Use conflicting when a claim is both
  partly supported and materially contradicted.

Conflict interpretation rules:
- Mark evidence as conflicting only when the contexts make mutually exclusive claims about the
  same object, property, scope, and conditions, such that both claims cannot be true together.
- Different wording, sections, values, test conditions, operating conditions, time periods, or
  scopes are not conflicts by themselves.
- Do not infer that one statement excludes another unless the contexts explicitly establish that
  relationship or the two claims are logically incompatible under the same conditions.
- Ambiguity about how two statements relate is not affirmative contradiction. Use uncertain only
  when that ambiguity materially prevents assessing an answer-essential claim.

Output consistency rules:
- Each of the three metric objects must contain its value, a concise reason explaining
  that specific value, and exactly the context_ids relevant to that observation. Do not write a
  generic overall reason in place of per-metric reasons.
- full or partial coverage requires supporting context IDs. conflicting consistency requires the
  context IDs that establish the conflict. limited or substantial noise requires the noise context
  IDs. none coverage or noise must use an empty context_ids list.
- Decompose each answer into concise material factual claims and assess them against contexts.
- Cite only context_id values present in the input; never invent or infer an ID.
- Every claim assessment must put its support level in value and include a concise reason.
- At claim level, context_ids are supporting evidence for full or partial, and contradictory
  evidence for conflicting. full, partial, and conflicting require context_ids; none and uncertain
  require an empty context_ids list.
- The system derives reference_answer_support and actual_answer_support from the claim assessments.
  Do not output either answer-level support field.
- Write reason and claim text in the language of the question when practical.
{empty_retrieval_rule}
{untrusted_input_notice()}

<evaluation_input>
{json_block(payload)}
</evaluation_input>"""
