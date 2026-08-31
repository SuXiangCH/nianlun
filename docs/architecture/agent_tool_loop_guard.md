# Nianlun Agent 工具循环护栏

## 背景与范围

Lead Agent 会在“模型调用 -> 工具执行”之间循环。仅依赖提示词和检索去重，无法阻止模型反复请求同一批工具；曾有批量任务运行约 52 分钟后才达到 LangGraph 的 9999 步上限。

`RetrievalLoopGuardMiddleware` 为单次请求设置运行时护栏：在保留已获得证据的前提下，阻止无效循环并让模型完成回答。它不判断答案正确性，不替代模型、Milvus 或其他外部服务的超时和重试，也不引入跨请求配额。

## 运行方式

护栏由 `AgentRuntimeFactory` 与现有 middleware 一起组装：

```text
ContextSummarizationMiddleware
DanglingToolCallMiddleware
ToolErrorHandlingMiddleware
RetrievalLoopGuardMiddleware
RetrievalDeduplicationMiddleware
ClarificationMiddleware
```

状态存放在每次 `AgentRequestContext` 新建的 `LoopGuardState` 中，不得放入模块全局变量或 middleware 实例。状态记录模型/工具轮次、调用指纹、已见证据、预算、告警和收尾状态；并行工具调用只在短暂更新这些内存状态时加锁。

护栏通过三个控制点工作：

- `after_model`：统计轮次、比较本轮工具调用组合，并决定是否告警或进入收尾。
- `wrap_tool_call`：预留允许的调用，为被阻止调用生成对应的 `ToolMessage`，并记录是否带来新证据。
- `wrap_model_call`：注入一次性告警；收尾时移除工具 schema，只允许基于已有证据回答。

## 循环与进展

调用指纹对参数做确定性规范化，因此字典字段和并行调用顺序变化不会规避重复检测。`get_line_content` 将 `doc_id`、`line_spec`、`char_offset` 与有效字符上限纳入指纹，允许正常分页，但阻止同一窗口重复读取。

一轮工具调用只要产生新的文档/节点、citation、正文窗口或文档定位信息，即视为有进展。空结果、去重后为空、工具错误和重复窗口都不算进展。连续无进展按工具轮次累计。

## 默认预算

| 限制项 | 告警 | 强制收尾 |
| --- | ---: | ---: |
| 相同调用组合或调用指纹 | 2 次 | 3 次 |
| 连续无新增证据 | 2 轮 | 3 轮 |
| 模型轮次 | 24 | 32 |
| 工具轮次 | 24 | 32 |
| 总工具调用 | 240 | 300 |

单工具上限：`search_document_nodes` 20、`find_semantic_documents` 4、`get_document` 50、`get_structure_outline` 50、`get_line_content` 200、`ask_clarification` 1。

额外兜底为 `recursion_limit=192` 和单题 `case_timeout_seconds=1200`。这些都是安全上限，不是建议的检索次数；持续获得新证据的跨文档问题应能在上限内正常完成。

## 告警与收尾

达到告警阈值时，本轮工具仍会执行；告警只在下一次模型调用临时注入，不写入会话历史或最终答案。

达到硬限制或超时后：

1. 超额调用不执行，但每个原始 `tool_call_id` 都收到配对的错误 `ToolMessage`。
2. 下一次模型调用隐藏全部工具，并要求仅基于已有证据作答。
3. 若模型仍返回工具调用，响应会被净化为不含工具调用的安全答案。
4. CLI 和 SSE 不得发布该受限模型轮的原始文本 chunk；只发布净化后的最终答案。

最终模型调用失败会抛出 `GuardFinalizationError`，批量执行不应自动重试该 case。普通聊天 API/SSE 不暴露护栏统计；批量结果和脱敏日志可记录 `guard` 摘要，用于调整阈值。

## 配置与验证

`AgentLoopGuardConfig` 是唯一配置边界，负责校验正数阈值、告警不超过硬限制、递归上限和允许的工具名。默认配置同时用于 CLI 与 Web。工具 schema 未变化时无需提升 `AGENT_TOOL_SCHEMA_VERSION`；提示词变更仍需更新 `PROMPT_VERSION`。

回归测试覆盖重复调用、无进展、并行工具配对、预算、超时、工具调用净化、流式输出、批量不可重试和请求隔离。修改护栏时至少运行：

```bash
uv run pytest tests/agent
make lint
```
