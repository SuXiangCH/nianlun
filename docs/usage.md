# Agentic RAG 使用说明

Nianlun 是多文档知识库问答系统：以 markdown 标题树索引导航和 Agent 按行号回读正文为主，并可选使用向量检索做语义文档路由。索引（离线）与检索/生成（在线）解耦，通过工作区 JSON 和 Milvus collection 对接。

> 根目录的 `README.md` 是项目总览；本文档是 `nianlun/` 系统（markdown 树索引 +
> LangChain Agent 检索/生成）的详细使用说明。

## 它由三部分组成

| 部分 | 路径 | 职责 |
|---|---|---|
| **索引** | `nianlun/indexing/tree/` | 把 markdown 解析成标题树索引 JSON（markdown-it-py 解析 + LangChain `ChatOpenAI` 生成节点摘要/文档描述）。离线。 |
| **检索/生成** | `nianlun/agent/` | LangChain 1.x Agent 的交互与批量入口；核心 Agent 位于 `nianlun/agent/lead_agent/`，读工作区 JSON，提供 FTS 工具和可选语义文档工具。在线。 |
| **模型层** | `nianlun/models/` | 统一的 Chat Model / Embedding Model 工厂与响应归一化，索引侧与检索侧共用。 |

> 索引侧只处理 markdown。PDF/Word 等先用上游转换器（如 MinerU）转成 `full.md` 再喂给本项目。

## 目录结构

```
nianlun/
  __init__.py            顶层包（瘦身，不 eager 导入运行时）
  config.py              共享模型和环境配置
  models/                模型适配层
    llm.py               共享 LLM 工厂 build_chat_model + content_to_text
    embedding.py         共享 Embedding 工厂、TextEmbedder 和批量向量化
  agent/                 交互与批量场景入口（在线）
    cli.py               交互式命令行入口
    batch.py             批量评测入口：并发/重试/增量写出
    lead_agent/          主 Agent 核心编排
      agent.py           Agent 工厂与应用绑定
      prompt.py          系统提示词与检索上下文
      runtime.py         应用级 AgentRuntime facade
      runner.py          单轮问答、流式输出和请求状态
      factory.py         Agent graph 与应用依赖组装
      routing.py         轻量意图路由（寒暄等）
    middleware/          Agent 横切能力
    tools/               LangChain 工具
  indexing/tree/         markdown 树索引（离线）
  indexing/fts/          节点级全文索引（离线）
  indexing/vector/       可选语义文档向量索引（离线）
  knowledgebase/         在线知识库访问与检索
    core.py              工作区加载、文档读取和检索操作
    full_text_retriever.py  全文节点检索适配器
    config.py            知识库路径和检索配置
tests/                    独立测试、parity 脚本和 golden 数据
  agent/
  indexing/fts/
  indexing/tree/golden/
  knowledgebase/
datasets/                每个数据集一个目录：full.md（待索引 markdown）+ images/ + MinerU 产物
data/workspaces/default/          默认知识库：每个文档一个 <doc_id>.json + _meta.json
train.json               批量测试题集（filename/page/question/answer）
results/                 批量测试输出（时间戳 JSONL）
```

## 一、环境准备

需要 Python 3.11+。项目使用 `uv` 管理环境和依赖，命令均从仓库根目录运行。

```bash
uv sync --all-groups
```

### 开发命令

项目命令通过 `uv run` 使用 `.venv` 中的解释器：

```bash
uv run python -c "import langchain; print(langchain.__version__)"
make format
make lint
make typecheck
make test
```

配置 `.env`（仓库根目录）：

```bash
OPENAI_API_KEY=sk-...                  # 必填
OPENAI_API_BASE=http://your-relay/v1   # 中转站地址（或用 OPENAI_BASE_URL）
# 可选：
# OPENAI_MODEL=Qwen3.6-35B-A3B-FP8     # 模型，缺省走 DEFAULT_MODEL
# OPENAI_ENABLE_THINKING=true          # 检索侧是否开思考模式（默认开；索引侧固定关）
# OPENAI_TEMPERATURE=0.8               # 检索侧采样温度（默认 0.8；索引侧固定 0）
```

> 索引侧 `temperature` 固定 0、`enable_thinking` 固定关（可复现、不浪费 output 预算）；检索侧由环境变量控制。

## 二、构建索引

仓库自带 `data/workspaces/default/`（已建好 135 篇文档索引），开箱即可问答。如需重建或新增，用 `tree_index` CLI（从仓库根运行）。

### 1. 单篇文档（打印 / 落盘索引 JSON）

```bash
# 打印到 stdout（零 LLM，纯结构，便于快速查看树形）
uv run python -m nianlun.indexing.tree.cli data/source/datasets/<某文档>/full.md --no-summary

# 带节点摘要 + 文档描述（调用 LLM），写到文件
uv run python -m nianlun.indexing.tree.cli data/source/datasets/<某文档>/full.md -o out.json

# 关闭 ATX_ONLY，启用 CommonMark 完整标题语义（认 setext 标题等）
uv run python -m nianlun.indexing.tree.cli data/source/datasets/<某文档>/full.md --full-commonmark -o out.json
```

| 选项 | 含义 |
|---|---|
| `paths` | markdown 路径；单文档模式即待索引的 `.md` |
| `-o, --out` | 输出 JSON 路径；不传则打印到 stdout |
| `--no-summary` | 纯结构索引，**零 LLM 调用**（不生成摘要/描述） |
| `--model` | 模型，缺省读 `OPENAI_MODEL`/`DEFAULT_MODEL` |
| `--full-commonmark` | 关闭 ATX_ONLY，认 setext 等完整 CommonMark 标题 |

### 2. 重建工作区（批量写知识库 JSON）

把多篇文档写进工作区目录（默认 `data/workspaces/default/`），供检索侧读取：

```bash
# 重建全部 133 篇（--clean 先清空旧 *.json，避免新旧并存）
uv run python -m nianlun.indexing.tree.cli \
  --reindex --workspace data/workspaces/default --clean data/source/datasets/*/full.md

# 不清空，增量追加新文档
uv run python -m nianlun.indexing.tree.cli \
  --reindex --workspace data/workspaces/default datasets/新文档/full.md
```

| 选项 | 含义 |
|---|---|
| `--reindex` | 重建工作区模式（`paths` 为待重建的 markdown 列表） |
| `--workspace` | 工作区目录（默认 `data/workspaces/default/`） |
| `--clean` | 重建前清空 workspace 下既有 `*.json`（防新旧并存） |
| `--no-summary` | 纯结构重建，零 LLM（快，但无摘要/描述） |

> 默认重建会调 LLM 生成每节点摘要 + 文档描述，133 篇会有较多调用。只想验证管线可用 `--no-summary`。

## 三、检索问答

### 1. 交互模式

```bash
# 流式输出（默认）
uv run python -m nianlun.agent.cli

# 非流式（整轮完成后一次性输出）
uv run python -m nianlun.agent.cli --no-stream
```

启动后输入问题，Agent 自主调用检索工具（查目录 / 取正文 / 跨文档搜索 / 看结构）后作答。输入 `list` 看文档清单，`trace` 看上一轮取回的正文。

系统提示词不会在 Agent 初始化时注入全部文档目录，文档路由改由
`search_document_nodes` 按需返回 `documents[].doc_id` 和
`documents[].node_hints[].line_num`。

### 2. 批量测试

用 `train.json`（题集：`filename`/`page`/`question`/`answer`）批量跑并评估：

```bash
# 4 并发、关工具日志、写到指定文件
uv run python -m nianlun.agent.cli \
  --batch-file train.json --workers 4 --quiet-tools --output results/run.jsonl

# 跑前 10 条
uv run python -m nianlun.agent.cli --batch-file train.json --limit 10

# 从第 20 条开始跑
uv run python -m nianlun.agent.cli --batch-file train.json --offset 20
```

| 选项 | 含义 |
|---|---|
| `--batch-file` | 题集文件，支持 JSON / JSONL |
| `--output` | 输出路径，默认 `results/` 下时间戳 JSONL |
| `--offset` / `--limit` | 起始记录 / 最多跑几条 |
| `--workers` | 并发 worker 数（按模型限流调节，默认 1） |
| `--retry-times` | 单题失败重试次数（默认 3） |
| `--quiet-tools` | 关闭每次工具调用的详细日志 |
| `--no-stream` | 关闭流式输出 |

## 四、测试

### tree_index 校验套件（不调真实模型）

全部用 `-m` 跑（从仓库根，无需 `PYTHONPATH`）：

```bash
uv run python tests/indexing/tree/parity_check.py        # line_num 0 偏差硬门禁
uv run python tests/indexing/tree/pipeline_parity.py     # 全量结构 + token 逐字节比对
uv run pytest tests/indexing/tree/test_pipeline_unit.py
uv run pytest tests/indexing/tree/test_llm_unit.py
uv run python tests/indexing/tree/orchestration_parity.py
uv run python tests/indexing/tree/workspace_smoke.py
```

- 前两个比对 `tests/indexing/tree/golden/` 下冻结的 133 篇旧实现输出（结构 133/133、节点 5290/5290、token 计数逐节点相等）。
- 全部走 markdown-it + tiktoken，**不调真实模型**。

### 真实模型验证（需手动跑，默认不自动执行）

```bash
# 单文档带摘要，端到端走真实模型
uv run python -m nianlun.indexing.tree.cli data/source/datasets/<某文档>/full.md -o out.json

# 真实模型 parity（按需 --limit）
uv run python tests/indexing/tree/real_model_parity.py --limit 3
```

## 五、工作区数据格式

`data/workspaces/default/` 下每个文档一个 `<doc_id>.json`，外加 `_meta.json` 注册表：

- `<doc_id>.json`：单文档树索引（`doc_name` / `line_count` / `doc_description` / `structure` 嵌套树，每个节点含 `title` / `line_num` / `level` / `summary` / `text` / `node_id` / `nodes`）。
- `_meta.json`：`{doc_id: {doc_name, doc_description, line_count, ...}}` 文档清单。

检索侧 `KnowledgeBase` 读这套格式；`get_line_content` 按 `line_num` 在索引 JSON 里匹配节点、返回节点存的 `text`（md 索引直接存全文，不回读源 `.md`）。

## 六、配置参考

| 环境变量 | 作用 | 默认 | 影响侧 |
|---|---|---|---|
| `OPENAI_API_KEY` | API 密钥（必填） | - | 两者 |
| `OPENAI_API_BASE` / `OPENAI_BASE_URL` | 中转站地址 | - | 两者 |
| `OPENAI_MODEL` | 模型名 | `Qwen3.6-35B-A3B-FP8` | 两者 |
| `OPENAI_ENABLE_THINKING` | 思考模式开关 | `true` | 检索侧（索引侧固定关） |
| `OPENAI_TEMPERATURE` | 采样温度 | `0.8` | 检索侧（索引侧固定 0） |

## 七、RAG 评估

评估器与 Nianlun 的 Agent 运行时解耦。任何 RAG 系统只要提供问题、标准答案、实际回答和
检索内容，就可以使用同一套 JSONL 协议评估答案正确性、检索证据与错误归因。

运行命令、模型配置、输入样例、Excel 导出和断点续跑见
[RAG Evaluation 使用说明](../nianlun/evaluation/README.md)；指标语义和输出契约见
[RAG 评估架构](architecture/evaluation.md)。
