from __future__ import annotations

import pytest
from pydantic import ValidationError

from nianlun.evaluation.contracts import (
    ATTRIBUTION_ANNOTATIONS,
    AnswerVerdict,
    AttributionCategory,
    AttributionStrength,
    ContextItem,
    EvaluationCase,
    EvaluationOutcome,
    HallucinationEvidence,
    MetricAssessment,
    EvidenceConsistency,
    RetrievalCoverage,
    RetrievalNoise,
    SupportLevel,
    case_fingerprint,
    normalize_contexts,
)
from nianlun.evaluation.stages.attribution.schema import (
    AttributionAssessment,
    AttributionRunRecord,
    HallucinatedClaim,
)
from nianlun.evaluation.stages.correctness.schema import (
    CorrectnessAssessment,
    ReferenceQualityAssessment,
)
from nianlun.evaluation.stages.critic.schema import CriticResult
from nianlun.evaluation.stages.evidence.schema import (
    ClaimEvidenceAssessment,
    EvidenceConsistencyAssessment,
    EvidenceModelOutput,
    EvidenceResult,
    RetrievalCoverageAssessment,
    RetrievalNoiseAssessment,
    SupportAssessment,
    derive_evidence_result,
    derive_support_assessment,
)


def test_case_is_strict_and_allows_empty_actual_answer() -> None:
    case = EvaluationCase(
        question="q",
        reference_answer="r",
        actual_answer="",
        retrieval_contexts=[],
    )
    assert case.is_empty_answer
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate({**case.model_dump(), "metadata": {}})


def test_result_schemas_do_not_expose_model_self_reported_confidence() -> None:
    for schema in (
        CorrectnessAssessment,
        ReferenceQualityAssessment,
        EvidenceResult,
        CriticResult,
        AttributionAssessment,
        EvaluationOutcome,
    ):
        assert "confidence" not in schema.model_json_schema()["properties"]
    assert (
        "verdict_confidence" not in EvaluationOutcome.model_json_schema()["properties"]
    )


def test_every_public_metric_assessment_exposes_value_and_reason() -> None:
    metric_schemas = (
        CorrectnessAssessment,
        ClaimEvidenceAssessment,
        RetrievalCoverageAssessment,
        RetrievalNoiseAssessment,
        EvidenceConsistencyAssessment,
        SupportAssessment,
        AttributionAssessment,
    )

    for schema in metric_schemas:
        assert issubclass(schema, MetricAssessment), schema.__name__
        properties = schema.model_json_schema()["properties"]
        assert "value" in properties, schema.__name__
        assert "reason" in properties, schema.__name__


def test_attribution_schema_describes_output_field_semantics() -> None:
    properties = AttributionAssessment.model_json_schema()["properties"]

    assert "primary" in properties["value"]["description"]
    assert "secondary" in properties["secondary_issues"]["description"]
    assert (
        "primary attribution value" in properties["attribution_strength"]["description"]
    )
    assert "directly linked" in properties["noise_context_ids"]["description"]


def test_metric_assessment_rejects_blank_reason() -> None:
    with pytest.raises(ValidationError, match="reason cannot be blank"):
        CorrectnessAssessment(value=AnswerVerdict.CORRECT, reason="   ")


def test_evidence_metrics_require_individual_reasons_and_context_ids() -> None:
    payload = {
        "retrieval_coverage": {
            "value": "full",
            "reason": "all required evidence is present",
            "context_ids": ["ctx-1"],
        },
        "retrieval_noise": {
            "value": "none",
            "reason": "no distracting context",
            "context_ids": [],
        },
        "evidence_consistency": {
            "value": "consistent",
            "reason": "the cited evidence agrees",
            "context_ids": ["ctx-1"],
        },
        "reference_claim_assessments": [
            {
                "value": "full",
                "reason": "the reference claim is supported",
                "claim": "reference claim",
                "context_ids": ["ctx-1"],
            }
        ],
        "actual_claim_assessments": [
            {
                "value": "full",
                "reason": "the actual claim is supported",
                "claim": "actual claim",
                "context_ids": ["ctx-1"],
            }
        ],
    }
    result = EvidenceModelOutput.model_validate(payload)
    assert result.retrieval_coverage.value == "full"

    missing_reason = {**payload, "retrieval_coverage": {"value": "full"}}
    with pytest.raises(ValidationError, match="retrieval_coverage.reason"):
        EvidenceModelOutput.model_validate(missing_reason)


def test_evidence_values_require_matching_citations() -> None:
    with pytest.raises(ValidationError, match="full support requires context_ids"):
        SupportAssessment(value=SupportLevel.FULL, reason="supported")
    with pytest.raises(ValidationError, match="conflicting support requires"):
        ClaimEvidenceAssessment(
            value=SupportLevel.CONFLICTING,
            reason="contradicted",
            claim="claim",
        )
    with pytest.raises(ValidationError, match="full requires claim assessments"):
        EvidenceResult.model_validate(
            {
                "retrieval_coverage": {
                    "value": "full",
                    "reason": "complete",
                    "context_ids": ["ctx-1"],
                },
                "retrieval_noise": {"value": "none", "reason": "no noise"},
                "evidence_consistency": {
                    "value": "consistent",
                    "reason": "consistent",
                },
                "reference_answer_support": {
                    "value": "full",
                    "reason": "supported",
                    "context_ids": ["ctx-1"],
                },
                "actual_answer_support": {
                    "value": "none",
                    "reason": "unsupported",
                },
            }
        )

    claim = ClaimEvidenceAssessment(
        value=SupportLevel.FULL,
        reason="ctx-1 supports the claim",
        claim="claim",
        context_ids=["ctx-1"],
    )
    with pytest.raises(ValidationError, match="must agree with its claim assessments"):
        EvidenceResult.model_validate(
            {
                "retrieval_coverage": {
                    "value": "full",
                    "reason": "complete",
                    "context_ids": ["ctx-1"],
                },
                "retrieval_noise": {"value": "none", "reason": "no noise"},
                "evidence_consistency": {
                    "value": "consistent",
                    "reason": "consistent",
                },
                "reference_answer_support": {
                    "value": "partial",
                    "reason": "partly supported",
                    "context_ids": ["ctx-1"],
                },
                "actual_answer_support": {
                    "value": "full",
                    "reason": "supported",
                    "context_ids": ["ctx-1"],
                },
                "reference_claim_assessments": [claim],
            }
        )


def test_support_is_derived_from_claims_with_deterministic_context_ids() -> None:
    claims = [
        ClaimEvidenceAssessment(
            value=SupportLevel.FULL,
            reason="first claim is supported",
            claim="first claim",
            context_ids=["ctx-1", "ctx-2"],
        ),
        ClaimEvidenceAssessment(
            value=SupportLevel.PARTIAL,
            reason="second claim is partially supported",
            claim="second claim",
            context_ids=["ctx-2", "ctx-3"],
        ),
    ]

    aggregate = derive_support_assessment(claims)

    assert aggregate.value is SupportLevel.PARTIAL
    assert aggregate.context_ids == ["ctx-1", "ctx-2", "ctx-3"]
    assert derive_support_assessment([]).value is SupportLevel.UNCERTAIN

    output = EvidenceModelOutput(
        retrieval_coverage=RetrievalCoverageAssessment(
            value=RetrievalCoverage.FULL,
            reason="all required evidence is present",
            context_ids=["ctx-1"],
        ),
        retrieval_noise=RetrievalNoiseAssessment(
            value=RetrievalNoise.NONE,
            reason="no distracting context",
        ),
        evidence_consistency=EvidenceConsistencyAssessment(
            value=EvidenceConsistency.CONSISTENT,
            reason="the evidence agrees",
        ),
        reference_claim_assessments=claims,
        actual_claim_assessments=claims,
    )

    result = derive_evidence_result(output)
    assert result.reference_answer_support.context_ids == ["ctx-1", "ctx-2", "ctx-3"]


def test_claim_assessment_accepts_legacy_evidence_id_fields() -> None:
    supported = ClaimEvidenceAssessment.model_validate(
        {
            "value": "full",
            "reason": "legacy support citation",
            "claim": "supported claim",
            "supporting_context_ids": ["ctx-1"],
            "conflicting_context_ids": [],
        }
    )
    conflicted = ClaimEvidenceAssessment.model_validate(
        {
            "value": "conflicting",
            "reason": "legacy conflicting citation",
            "claim": "conflicted claim",
            "supporting_context_ids": ["ctx-1"],
            "conflicting_context_ids": ["ctx-2"],
        }
    )

    assert supported.context_ids == ["ctx-1"]
    assert conflicted.context_ids == ["ctx-2"]
    assert (
        "supporting_context_ids"
        not in ClaimEvidenceAssessment.model_json_schema()["properties"]
    )


def test_zero_call_completed_outcome_only_allows_empty_answer_contract() -> None:
    from nianlun.evaluation import RagEvaluator
    from nianlun.evaluation.contracts import JudgeMetadata

    class NeverJudge:
        metadata = JudgeMetadata(provider="fake", model="fake", temperature=0.0)

    case = EvaluationCase(
        question="q",
        reference_answer="r",
        actual_answer="",
        retrieval_contexts=[],
    )

    import asyncio

    result = asyncio.run(RagEvaluator(judge=NeverJudge()).evaluate(case))  # type: ignore[arg-type]
    payload = result.model_dump(mode="json")
    payload["correctness"]["value"] = "correct"
    payload["attribution"] = None

    with pytest.raises(ValidationError, match="zero-call completed outcome"):
        EvaluationOutcome.model_validate(payload)


def test_attribution_annotations_cover_every_category() -> None:
    assert set(ATTRIBUTION_ANNOTATIONS) == set(AttributionCategory)
    for annotation in ATTRIBUTION_ANNOTATIONS.values():
        assert all(
            value.strip()
            for value in (
                annotation.label_zh,
                annotation.label_en,
                annotation.description_zh,
                annotation.description_en,
            )
        )


def test_context_ids_are_normalized_stably_and_duplicates_rejected() -> None:
    case = EvaluationCase(
        question="q",
        reference_answer="r",
        actual_answer="a",
        retrieval_contexts=[ContextItem(text="one"), ContextItem(text="two")],
    )
    normalized = normalize_contexts(case)
    assert [item.context_id for item in normalized.retrieval_contexts] == [
        "ctx-1",
        "ctx-2",
    ]
    assert case_fingerprint(normalized) == case_fingerprint(normalize_contexts(case))
    duplicated = case.model_copy(
        update={
            "retrieval_contexts": [
                ContextItem(text="one", context_id="same"),
                ContextItem(text="two", context_id="same"),
            ]
        }
    )
    with pytest.raises(ValueError, match="duplicate context_id"):
        normalize_contexts(duplicated)


def test_hallucination_and_unknown_attribution_constraints() -> None:
    with pytest.raises(ValidationError, match="hallucination attribution"):
        AttributionAssessment(
            value=AttributionCategory.HALLUCINATION,
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="x",
        )
    with pytest.raises(ValidationError, match="unknown attribution"):
        AttributionAssessment(
            value=AttributionCategory.UNKNOWN,
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="x",
        )
    claim = HallucinatedClaim(
        claim="wrong year",
        evidence=HallucinationEvidence.CONTRADICTED,
        context_ids=["ctx-1"],
    )
    assert claim.context_ids == ["ctx-1"]

    with pytest.raises(ValidationError, match="contradictory context citation"):
        HallucinatedClaim(
            claim="reference-only disagreement",
            evidence=HallucinationEvidence.CONTRADICTED,
            contradicted_by_reference=True,
        )

    with pytest.raises(ValidationError, match="hallucination attribution"):
        AttributionAssessment(
            value=AttributionCategory.RETRIEVAL_INCOMPLETE,
            secondary_issues=[AttributionCategory.HALLUCINATION],
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="x",
        )


def test_attribution_assessment_enforces_unknown_strength_and_unique_issues() -> None:
    with pytest.raises(ValidationError, match="insufficient strength"):
        AttributionAssessment(
            value=AttributionCategory.UNKNOWN,
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="cannot determine the cause",
        )
    with pytest.raises(ValidationError, match="duplicates"):
        AttributionAssessment(
            value=AttributionCategory.RETRIEVAL_INCOMPLETE,
            secondary_issues=[
                AttributionCategory.HALLUCINATION,
                AttributionCategory.HALLUCINATION,
            ],
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="retrieval is incomplete",
        )


def test_attribution_run_record_contains_only_routing_metadata() -> None:
    record = AttributionRunRecord(
        allowed_attributions=[AttributionCategory.RETRIEVAL_MISSING],
        deterministic=True,
    )
    assert record.deterministic
    with pytest.raises(ValidationError, match="Extra inputs"):
        AttributionRunRecord.model_validate(
            {
                "allowed_attributions": ["retrieval_missing"],
                "deterministic": False,
                "result": {},
            }
        )
