"""Answer-correctness stage implementation."""

from nianlun.evaluation.contracts.case import EvaluationCase
from nianlun.evaluation.judge.runtime import EvaluationRuntime, StructuredGeneration
from nianlun.evaluation.stages.correctness.prompt import build_prompt
from nianlun.evaluation.stages.correctness.schema import CorrectnessResult


class Correctness:
    def __init__(self, runtime: EvaluationRuntime) -> None:
        self.runtime = runtime

    async def evaluate(
        self,
        case: EvaluationCase,
    ) -> StructuredGeneration[CorrectnessResult]:
        return await self.runtime.generate_structured(
            build_prompt(case),
            CorrectnessResult,
        )


if __name__ == "__main__":
    import json

    json_schema = json.dumps(
        CorrectnessResult.model_json_schema(), ensure_ascii=False, indent=2, default=str
    )
    print(json_schema)
