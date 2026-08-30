"""Prompt for the independent answer correctness evaluator."""

from __future__ import annotations

from nianlun.evaluation.contracts.case import EvaluationCase
from nianlun.evaluation.stages.common import (
    json_block,
    untrusted_input_notice,
)

PROMPT_VERSION = "2026-08-20.v3"


def build_prompt(case: EvaluationCase) -> str:
    payload = {
        "question": case.question,
        "reference_answer": case.reference_answer,
        "actual_answer": case.actual_answer,
    }
    return f"""You are an expert evaluator responsible for determining whether an actual answer
correctly and completely answers the given question.

Use the reference answer as an important comparison basis, but assess its quality independently
because it may be incomplete, conflicting, or insufficient. Do not diagnose why an error occurred.

Evaluation procedure:
1. Identify every explicit requirement in the question.
2. Identify the answer-essential conclusions and facts in the reference answer.
3. Compare the actual answer by semantic meaning rather than wording or sentence order.
4. Separate errors in the central answer from optional omissions and harmless extra detail.
5. Put the answer verdict and its reason in correctness; assess reference_quality independently
   and give that assessment its own reason.

Answer verdict boundaries:
- A correct answer satisfies every core requirement, contains no material factual or logical
  error, and may omit optional detail. Compatible information beyond the reference is allowed.
- A partially_correct answer gives a useful and substantially correct central answer but has a
  material secondary omission or a localized error that does not invalidate the central answer.
- An incorrect answer has a wrong central conclusion, fails the main request, materially
  contradicts the comparison basis, or is misleading enough to make the answer unusable.
- Use uncertain only when ambiguity or insufficient supplied information prevents a stable
  verdict. Do not use uncertain merely because the comparison is difficult.

Reference quality boundaries:
- adequate: sufficient, relevant, and internally consistent for judging the question.
- incomplete: usable but missing information needed to judge one or more core requirements.
- conflicting: contains a material internal contradiction or conflicts with the question.
- unknown: its quality cannot be determined from the supplied input.

Decision rules:
- Do not penalize style, verbosity, concision, formatting, or semantically equivalent wording.
- Information absent from the reference answer is not automatically false.
- Do not lower reference_quality merely because the actual answer disagrees with the reference;
  require an identifiable defect in the reference itself. A concise reference can be adequate.
- A minor optional omission does not justify partially_correct.
- A defect that changes the requested central conclusion is not merely partially_correct.
- matched_facts, missing_facts, and incorrect_claims must directly support the verdict, contain
  concise factual items, and not contradict one another.
- correctness.value is the answer verdict and must be one of the answer verdict values above.
- reference_quality.value must use the reference-quality boundaries above. Its reason must explain
  the quality of the reference itself, not repeat the answer-correctness reason.
- Write reason and factual list items in the language of the question when practical.
{untrusted_input_notice()}

<evaluation_input>
{json_block(payload)}
</evaluation_input>"""
