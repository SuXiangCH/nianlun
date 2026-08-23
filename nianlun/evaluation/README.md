# RAG Evaluation 使用说明

`nianlun.evaluation` 使用统一的 JSONL 协议评估 RAG 结果，不依赖 Nianlun 的
Agent 运行时。因此任何 RAG 系统只要转换到该输入格式，都可以复用同一套评估器。

本文只说明如何运行评估。阶段职责、指标边界和稳定契约见
[RAG 评估架构](../../docs/architecture/evaluation.md)。

非空回答依次经过答案正确性评审、独立证据审查、Critic 复核；最终并非完全正确时，
还会进行受约束的错误归因。空 `actual_answer` 则由确定性规则直接判定为
`incorrect` 和 `generation_empty`，不会调用模型。

## 准备环境

在仓库根目录运行：

```bash
uv sync --all-groups
```

CLI 调用 OpenAI-compatible Chat Completions 接口。可以在 shell 中设置，或写入
仓库根目录的 `.env`（启动时自动读取）：

```bash
export OPENAI_API_KEY='your-api-key'
export OPENAI_BASE_URL='https://your-provider.example/v1'
```

未设置 `OPENAI_BASE_URL` 时，兼容读取 `OPENAI_API_BASE`。不要在命令行参数中
传入密钥，避免其留在 shell 历史或进程列表中。

每次运行都必须通过 `--judge-model` 显式指定裁判模型。取值应是供应商要求的
模型或部署名称，例如：

```bash
--judge-model deepseek-v4-flash-0731
```

评估器固定使用 `temperature=0.0`，默认不向供应商发送任何思考模式参数，由模型端采用
自身默认行为。只有显式设置 `OPENAI_ENABLE_THINKING=false` 时，当前
OpenAI-compatible 适配器才发送关闭配置；设置为 `true` 与不设置的传输行为相同。
不同供应商关闭思考模式的参数可能不同，接入新供应商时应在模型适配层实现对应策略。
`OPENAI_MODEL` 与 `OPENAI_TEMPERATURE` 不会覆盖 Judge 的模型名和温度；API key 和
base URL 仍然生效。

## 输入数据集

输入为 UTF-8 JSONL：每一行是独立的一条 `EvaluationCase` JSON 对象。顶层严格
只能有以下四个字段：

```json
{
  "question": "频率大于 10 kHz 时的修正系数是多少？",
  "reference_answer": "修正系数为 1.4。",
  "actual_answer": "可以正常使用，修正系数为 1.4。",
  "retrieval_contexts": [
    {
      "context_id": "ctx-1",
      "text": "| 频率 | >10 kHz |\\n| 系数 | 1.4 |",
      "source_id": "可选的文档 ID",
      "source_name": "可选的来源名称",
      "location": "可选的定位信息",
      "score": 0.92
    }
  ]
}
```

- `question` 与 `reference_answer` 必须是非空字符串。
- `actual_answer` 必须是字符串，但允许为空。
- `retrieval_contexts` 必须始终是列表。空列表表示 RAG 系统没有返回检索结果。
- 每个 context 必须有非空 `text`。`context_id` 可以省略，评估器会自动分配稳定
  ID；如果系统本身有 citation 或 chunk ID，应当传入，便于结果中的引用可追溯。
- 顶层不要加入 metadata 或任何系统私有字段。业务 ID、系统版本、人工标签等应通过
  input line 或 case fingerprint 维护在外部 sidecar 映射中。

`evals/datasets/material_selection_cases.jsonl` 是从物料选型人工标注 Excel
生成的可直接运行示例。Excel 中按 `分片N` 分隔的检索内容已经拆成
`retrieval_contexts` 列表。

## 先跑几个 Case

CLI 会评估输入中的每个非空行。先从完整数据集中提取前三行，使用独立输出路径做
最小 smoke test：

```bash
mkdir -p evals/results
sed -n '1,3p' evals/datasets/material_selection_cases.jsonl \
  > evals/results/material_selection_smoke_input.jsonl

uv run python -m nianlun.evaluation.cli \
  --input evals/results/material_selection_smoke_input.jsonl \
  --output evals/results/material_selection_smoke_output.jsonl \
  --judge-model deepseek-v4-flash-0731 \
  --workers 1
```

CLI 默认将评估阶段和批量进度日志输出到终端，包括总题数、当前进度、最终 verdict、
归因、失败阶段、跳过数量和耗时。日志不会输出问题、答案、检索正文或模型请求原文。

首次验证供应商配置时使用 `--workers 1`。确认成功后，再在供应商的限流和并发配额
内提高并发：

```bash
uv run python -m nianlun.evaluation.cli \
  --input evals/datasets/material_selection_cases.jsonl \
  --output evals/results/material_selection_output.jsonl \
  --judge-model deepseek-v4-flash-0731 \
  --workers 4 \
  --max-context-chars 12000 \
  --max-context-items 50
```

上下文限制按单个 case 在模型调用前生效。超过任一限制时，评估器只使用保留的上下文，
并在结果中记录发生了截断；这种结果不能被解读为“完整证据已被评估”。

Judge 模型默认使用供应商的思考模式默认值。评估用的裁判模型不支持思考输出时，设置
环境变量 `OPENAI_ENABLE_THINKING=false` 可显式关闭（仅识别显式 false，其余值不覆盖）；
关闭与否会影响 evaluator fingerprint，混合配置的结果不能放在一起汇总。

对于历史 Nianlun 批量问答结果，应使用显式 Adapter，而不是把原字段直接传给统一
协议：

```bash
uv run python -m nianlun.evaluation.cli \
  --input path/to/nianlun-results.jsonl \
  --output evals/results/nianlun-evaluation.jsonl \
  --judge-model deepseek-v4-flash-0731 \
  --adapter nianlun
```

## 查看结果与断点续跑

输出同样是 JSONL。每条成功记录具有以下 envelope：

```json
{
  "input_line": 1,
  "result": {
    "evaluation_status": "completed",
    "correctness": {
      "value": "correct",
      "reason": "...",
      "matched_facts": [],
      "missing_facts": [],
      "incorrect_claims": []
    },
    "reference_quality": {
      "value": "adequate",
      "reason": "..."
    },
    "evidence": {"...": "..."},
    "attribution": null,
    "run_logs": {"...": "..."}
  }
}
```

`correctness.value` 的取值是 `correct`、`partially_correct`、`incorrect` 或
`uncertain`。已完成的 `partially_correct` 与 `incorrect` 结果会包含
`attribution`；正确或不确定的结果不包含归因。失败记录不会给出最终结论，而是包含
`error`。

日常复核时，优先查看顶层字段，不需要展开 `run_logs`：

| 字段 | 何时查看 | 含义 |
| --- | --- | --- |
| `correctness.value`、`correctness.reason` | 每条完成记录 | 最终答案结论及其端到端依据，是首要结果。 |
| `attribution.value`、`attribution.reason` | verdict 为 `partially_correct` 或 `incorrect` | 最可能的错误位置及其依据，不等同于已经证明的因果事实。 |
| `reference_quality` | 标准答案可能有问题时 | 标准答案是否适合作为判断基准。 |
| `evidence` | 需要检查检索质量或归因是否合理时 | 覆盖、噪声、冲突、答案 claim 支持度及其引用。 |
| `evaluation_status`、`error` | 每条记录 | 区分完成的判断与评估器自身失败。失败记录没有最终结论。 |

`run_logs` 是完整 JSONL 中的审计元数据，包含版本、路由、调用量、重试、耗时和中间阶段记录，
用于复现、排障和断点续跑。成功记录不会在其中重复顶层的最终结论；Excel 导出也会刻意省略这些
调试字段，仅保留人工逐行复核需要的内容。

需要人工逐行复核时，可以在运行时指定 `--excel-output`。导出的单个工作表只包含输入行号、
问题、参考答案、实际答案、最终 verdict/attribution 及其 reason、状态和错误码；不会包含
检索上下文、claim 明细、路由、重试或 token 等调试字段：

```bash
uv run python -m nianlun.evaluation.cli \
  --input evals/datasets/material_selection_cases.jsonl \
  --output evals/results/material_selection_output.jsonl \
  --excel-output evals/results/material_selection_report.xlsx \
  --max-context-chars 120000 \
  --max-context-items 50 \
  --workers 3 \
  --judge-model deepseek-v4-flash
```

批处理器以追加方式写入输出文件。再次运行时，只有此前已完成且 evaluator
fingerprint 相同的 case 才会跳过，与已完成记录重复的输入行会写出 `skipped`
记录（`duplicate_of_input_line` 指向原始输入行）；`failed` 记录会被重新评估，
因此输出文件可能包含同一 case 的多条记录，读取时应按 case fingerprint 取
最后一条。fingerprint 覆盖模型元数据、提示词版本、Schema、
思考策略、脱敏端点标识、结构化输出协议和其他行为配置。需要独立运行或修改了评估配置
时，应使用新的输出文件路径。

使用下面的命令查看简化后的结果：

```bash
uv run python - <<'PY'
import json
from pathlib import Path

for line in Path("evals/results/material_selection_smoke_output.jsonl").read_text(
    encoding="utf-8"
).splitlines():
    payload = json.loads(line)
    result = payload.get("result")
    if result is None:
        print(payload)
        continue
    print({
        "input_line": payload["input_line"],
        "status": result["evaluation_status"],
        "verdict": result["correctness"]["value"] if result["correctness"] else None,
        "reason": result["correctness"]["reason"] if result["correctness"] else None,
        "attribution": (
            result["attribution"]["value"] if result["attribution"] else None
        ),
    })
PY
```

评估器是辅助人工判断的工具，而不是自动真值来源。任何提示词、模型或配置改动带来的
质量提升，都应使用独立的人工标注验收集验证。
