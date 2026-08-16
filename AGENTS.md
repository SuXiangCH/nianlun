# Nianlun Agent Rules

本文件适用于整个仓库。它面向在本项目中修改代码、测试和文档的自动化 Agent，也可作为贡献者的工程约定。若子目录以后出现更具体的 `AGENTS.md`，该文件只补充或收紧对应目录的规则。

## 1. 工作原则

- 先阅读与任务直接相关的实现、调用方和测试，再修改代码。不要根据文件名猜测契约。
- 保持改动聚焦。不要顺手重构、批量格式化或重命名与任务无关的代码。
- 优先沿用现有分层、类型和辅助函数；只有在确实减少复杂度或重复时才新增抽象。
- 修复问题时补回归测试；新增行为时同时覆盖成功路径、边界条件和主要失败路径。
- 不掩盖异常，不用宽泛的 `except Exception`、静默回退或测试降级来制造成功结果。现有兼容性回退除外，但修改时必须保留可观测性。
- 保持工作区中已有的用户改动。不要回滚、覆盖或删除来源不明的修改。
- 未经明确要求，不创建提交、不改写 Git 历史、不执行破坏性 Git 命令。

## 2. 项目结构与职责

```text
nianlun/
  agent/                 Agent 运行时、middleware、工具、主/子 Agent
  indexing/tree/         Markdown 解析和树索引
  indexing/fts/          全文检索记录与 Milvus FTS 索引
  indexing/vector/       向量记录、Embedding 与向量索引
  knowledgebase/         面向 Agent 的统一知识库与检索接口
  models/                LLM 和 Embedding 适配
app/api_server/
  apis/v1/               HTTP 路由与 Pydantic 请求/响应契约
  services/              应用编排和业务逻辑
  repositories/          SQLite 持久化边界
  database/              ORM、连接与迁移
  integrations/          MinerU 等外部服务适配
app/frontend/
  src/api/               HTTP 客户端与 SSE 协议解析
  src/features/          按业务功能组织的 React 页面
  src/components/        可复用 UI 组件
tests/                   Python 单元与服务层测试
data/                    本地数据库、上传文件和索引产物，不提交
evals/results/           批量运行输出，不提交
```

遵守以下依赖方向：

- HTTP 路由负责校验、鉴权边界（如将来加入）和响应转换，业务编排放在 `services/`。
- `services/` 通过 `repositories/` 和领域接口访问持久化或检索能力，不把 SQL、文件布局或 HTTP 细节泄漏到 Agent 核心。
- `nianlun/agent/` 依赖 `KnowledgeBasePort` 等稳定端口，不直接依赖 FastAPI、SQLite repository 或前端结构。
- FTS 与向量索引是可重建的派生产物；树工作区和文档元数据是构建它们的来源，不要反转这一关系。
- 前端通过 `src/api/` 访问服务端。组件中不要散落重复的 `fetch`、API base URL 或 SSE 解析逻辑。

## 3. 必须保持的契约

### Agent 与检索

- `nianlun/agent/contracts.py` 是 Agent 与知识库、请求上下文之间的稳定边界。修改端口时同步检查 factory、runner、工具和测试替身。
- 工具 schema 或其语义发生不兼容变化时，评估并更新 `AGENT_TOOL_SCHEMA_VERSION`。
- 每次请求的 collector、去重状态、澄清状态和 status sink 必须保持请求级隔离，不得放入跨请求共享的可变全局状态。
- 主 Agent 与子 Agent 之间只传递 `DeepSearchResult` 定义的有界、可序列化结果。不要让原始消息历史、runtime 对象或无限长证据跨越边界。
- 修改 prompt、工具描述、路由或 middleware 顺序属于行为变更，必须运行 `tests/agent/` 中相关测试；不要把 prompt 文案改动视为纯文档改动。
- 保留检索证据的定位字段，如 `doc_id`、`node_id`、`line_spec`、字符偏移和截断信息。回答质量不能以牺牲可追溯性为代价。

### 索引与知识库

- 树索引、FTS 和向量索引共享的文档标识、内容版本及记录字段必须保持一致。修改记录生成逻辑时检查构建、存储、检索和删除路径。
- FTS 和向量能力均可能被关闭或暂时不可用。不要让可选外部服务在模块导入或普通单元测试期间建立网络连接。
- 保留当前 FTS 不可用时的本地扫描降级语义，除非任务明确要求改变产品行为；降级不得伪装成远端索引构建成功。
- 索引重建、增量更新和文档删除必须成对考虑，避免 SQLite 元数据、工作区文件和 Milvus collection 之间残留孤立数据。
- 解析或切块规则变化时，优先加入小型确定性 fixture；不要用真实大文档或真实模型调用替代单元测试。

### API 与持久化

- `app/api_server/apis/v1/schemas.py` 是 v1 API 的服务端契约，默认拒绝额外字段。新增或修改字段时同步更新路由、service、前端类型/API 客户端及契约测试。
- 保持现有 JSON envelope、错误信息、`X-Request-Id` 和 HTTP 状态码语义。不要在单个 endpoint 中创造特殊返回格式。
- SSE 是公开协议。修改事件名、payload、顺序或结束/错误行为时，同时检查 `common/sse.py`、chat route、前端 `src/api/sse.ts` 和两端测试。
- 数据库 schema 变化必须通过 `database/migrations.py` 提供可重复执行的向前迁移，并覆盖已有数据库升级场景；不能只修改 ORM model 或初始化建表 SQL。
- repository 负责事务和持久化细节。跨资源操作应由 service 编排，并明确失败时的一致性与补偿行为。
- API key 只允许写入和内部使用。不得出现在响应、日志、异常、测试快照或提交的 fixture 中。

### 配置

- CLI 配置从 `nianlun/config.py` 和环境变量读取。
- Web 端的模型 Profile 以 SQLite 模型目录为准；`ApiServerSettings` 主要负责服务、存储和基础设施配置。不要让 Web 请求悄悄回退到 CLI 的模型环境变量。
- 新增环境变量时同步更新 `.env.example` 和相关配置测试；提供安全、可本地运行的默认值，密钥除外。
- 路径应基于 `PROJECT_ROOT` 或注入的 settings 解析，避免依赖当前 shell 所在目录。

### 前端

- 保持 TypeScript 类型与服务端 snake_case 字段一致；不要用无约束的类型断言掩盖契约不匹配。
- 保留请求取消能力。上传和流式聊天中的 `AbortSignal`、abort 状态及资源清理不可因重构丢失。
- SSE 解析必须正确处理分块边界、CRLF/LF、多行 `data:`、注释行和流结束时的剩余 buffer。
- 页面逻辑放入对应 `src/features/<feature>/`；仅在多个功能确实复用时提取到 `src/components/`。
- UI 改动沿用现有工作台式信息架构和视觉语言，并检查空状态、加载、错误、禁用与窄屏状态。

## 4. 数据与外部服务安全

- 不提交 `.env`、API key、token、用户上传、SQLite 数据库、Milvus 数据、`data/`、批量运行结果或模型生成的临时产物。
- 未经明确授权，不删除或清空 `data/`、工作区索引、数据库、collection 或用户文档。
- 测试默认使用临时目录、内存/临时 SQLite 和 mock/fake 外部依赖。不要为普通测试调用真实 LLM、Embedding、MinerU 或 Milvus。
- 涉及真实模型或外部服务的 parity、smoke、批量问答脚本不是默认测试的一部分；只有任务明确要求且凭据、成本与服务条件具备时才运行。
- 日志和异常中应包含可定位信息，但必须脱敏凭据、Authorization header、文档敏感内容和完整模型请求。

## 5. 实现与代码风格

- Python 目标版本为 3.11；使用现有类型注解风格，公共边界优先明确返回类型。
- 数据契约优先使用 Pydantic model、dataclass、TypedDict 或 Protocol，不使用松散字典约定替代已有结构。
- 保持 async/sync 边界清晰。不要在 async route 中直接执行长期阻塞调用；沿用现有 service 或 worker 机制。
- 注释解释约束、原因或非显然行为，不复述代码。中英文选择与邻近文件保持一致。
- 前端沿用现有 React、TypeScript 和原生 CSS 方案；没有明确收益时不引入新的状态管理、请求或 UI 框架。
- 新依赖必须有直接用途。Python 依赖变更同步更新 `pyproject.toml` 与 `uv.lock`；前端依赖变更同步更新 `package.json` 与 `package-lock.json`。

## 6. 推荐工作流

1. 用 `rg` 定位定义、调用方和测试，确认变更涉及的契约与持久化边界。
2. 先写或更新能复现目标行为的最小测试，再实现改动；纯重构也应由现有测试保护。
3. 先运行最接近改动的测试，再执行对应子系统的静态检查。
4. 检查 `git diff`，排除无关格式化、生成文件、密钥和运行时数据。
5. 在交付说明中列出行为变化、验证命令，以及未运行测试的原因和剩余风险。

## 7. 验证命令

所有 Python 命令从仓库根目录运行：

```bash
# 安装全部开发依赖
uv sync --all-groups

# 定向测试，优先在开发过程中使用
uv run pytest tests/agent/test_subagents.py
uv run pytest tests/api_server/test_api.py
uv run pytest tests/indexing/tree/test_pipeline_unit.py

# Python 质量检查
make lint
make typecheck
make test
```

前端命令从 `app/frontend/` 运行：

```bash
npm test
npm run typecheck
npm run build
```

按改动范围选择最低充分验证：

| 改动范围 | 至少运行 |
| --- | --- |
| 文档或注释 | 检查 diff；代码未变可不跑测试 |
| Agent、prompt、middleware、工具 | 对应 `tests/agent/` 测试 + `make lint` |
| tree/FTS/vector/knowledgebase | 对应索引测试；共享契约变化再跑全部相关检索测试 |
| API schema、service、repository、migration | 对应 `tests/api_server/` 测试 + `make typecheck` |
| 前端组件或状态逻辑 | 相关 Vitest + `npm run typecheck` |
| API/SSE 跨端契约 | Python API 测试 + 前端测试 + `npm run build` |
| 依赖、公共类型或跨层重构 | `make lint`、`make typecheck`、`make test`、前端全套检查 |

`make format` 会修改文件，只在准备接受其全部格式化结果时运行。验证阶段优先使用不会改写文件的 `make lint`。

## 8. 完成标准

任务只有在以下条件满足时才算完成：

- 请求的行为已经实现，且没有用临时硬编码或跳过校验代替正式方案。
- 相关契约、调用方、类型和文档保持一致。
- 新增或修改行为有与风险相称的测试覆盖。
- 已运行适当的验证命令，结果明确；无法运行的检查及原因已说明。
- diff 中不包含无关修改、敏感信息、运行时数据或生成产物。
