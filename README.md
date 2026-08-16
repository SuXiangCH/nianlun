# Nianlun

让数据沉淀为知识，让知识持续生长。

> 如树木之年轮，知识在时间的沉淀中层层生长，每一圈都有迹可循、有度可量。

Nianlun（年轮）是一个面向复杂知识场景的结构化 Agentic RAG 系统。不同于将文档预先切成固定长度、彼此割裂的 chunk，Nianlun 采用 No-Chunk 的组织方式：保留文档原有层级，并为缺少清晰标题的内容重建可导航的语义树。全文检索与可选的向量检索只负责定位候选文档和章节，Agent 再沿文档结构自主规划、按需读取相关原文并交叉核对，最终生成带有可追溯证据的回答。项目覆盖从数据入库、结构化索引到 Agent 问答的完整链路，让每一次回答都能回到原文。

当前项目包含多文档树索引、Milvus 全文检索、可选的语义向量索引、Agent 问答运行时、数据入库与批量问答工具。代码包位于 `nianlun/`，开源数据集位于 `datasets/`；索引产物、数据库等运行时数据写入 `data/`（不提交）。

## 为什么叫 Nianlun

Nianlun，取意“年轮”。我们相信，智能系统的进化不是一场爆炸，而是像树木一样，在时间中层层沉淀，在结构中自然生长。

| 核心能力 | 年轮意象 | 在 Nianlun 中的含义 |
| --- | --- | --- |
| Agent | 生长者 | Agent 如树木在森林中自主生长，向光而行、向深处扎根；它在复杂问题中持续探索，规划检索路径并调用工具完成任务。 |
| RAG | 根系汲取 | RAG 如根系穿透土壤，从多文档知识库中精准汲取养分；树索引、全文检索与向量检索共同为回答提供可追溯的证据。 |
| 数据治理 | 木质结构 | 数据治理如年轮的木质纹理，层层分明、疏密有序；文档解析、工作区、元数据和分层索引让每条数据在正确的圈层中归位。 |

Agent 是生长的意志，自主地向未知延伸；RAG 是深扎的根系，从知识的土壤中提取养分；数据治理是木质的分层，让混乱归于秩序。

在 Nianlun，我们不追求一蹴而就的参天，只在乎每一圈都扎实、每一层都可追溯。

## 环境准备

两种使用方式共用的准备步骤（需要 Python 3.11+，命令从仓库根目录运行）：

```bash
uv sync --all-groups
cp .env.example .env   # CLI 问答需填 OPENAI_API_KEY；纯 Web 端可跳过（模型在界面配置）
```

默认配置：

- Agent 默认模型：`deepseek-v4-flash-0731`（`OPENAI_MODEL` 可覆盖）
- 默认搜索模式：Milvus FTS；Milvus 不可用时自动降级为本地扫描
- 索引产物写入 `data/workspaces/default/`（运行时生成，`data/` 不提交）
- Milvus FTS 默认 collection：读取 `MILVUS_NODE_FTS_COLLECTION`；API Server 以 SQLite 中记录的 collection 为准

## 使用方法

### 方式一：Web 端（API Server + 前端）

适合日常使用：在界面上完成模型配置、文档上传解析、知识库管理和流式对话。

```bash
# 终端 1：启动 API Server（默认 http://127.0.0.1:8000，接口文档见 /docs）
uv run uvicorn app.api_server.main:app --reload

# 终端 2：启动前端（默认 http://127.0.0.1:3000）
cd app/frontend
npm install
npm run dev
```

> 前端默认连接 `http://127.0.0.1:8000`，可通过 `VITE_API_BASE` 覆盖。

使用流程：

1. **导入数据集（可选，替代手动上传）**：`uv run python -m app.api_server.import_datasets --knowledge-base 财报数据集 --create`——把 `datasets/` 的全部财报（Markdown + 预建树，含 LLM 摘要）批量灌入知识库，零模型调用，前端直接可见；导入后自动构建 FTS（需 Milvus 可达，不可达时文档仍可见、FTS 可稍后重建）。详见 [`datasets/README.md`](datasets/README.md)。
2. **模型管理**：配置 LLM Profile（provider / model / api_key / base_url），可选配置 Embedding 与 MinerU 解析器。模型目录存于 SQLite，是 API Server 唯一的模型配置来源（不依赖 `.env` 中的模型项）。
3. **知识库**：创建知识库并上传文档。Markdown 直接解析入库；PDF / Word 经 MinerU（SaaS 或私有化部署）转换为 Markdown 后入库，入库后自动构建 FTS 索引，可按需再开启向量索引。
4. **应用**：创建应用，绑定知识库与模型，选择检索模式。
5. **对话**：在对话页选择应用提问，流式输出回答并展示命中的证据片段。

### 方式二：纯 CLI

适合脚本化批处理、离线索引和批量测试，不启动任何服务。

```bash
# 1. 准备数据：使用 datasets/ 下的开源数据集，或自行放入 markdown
#    一文一目录：datasets/<doc-name>/full.md

# 2. 构建标题树索引（写工作区 JSON）
#    捷径：datasets/workspace/ 已含带 LLM 摘要的预建树，执行
#    mkdir -p data/workspaces && cp -r datasets/workspace data/workspaces/default
#    后可跳过本步（注意：目标目录已存在时 cp 会拷成子目录，需先清空再拷）
uv run python -m nianlun.indexing.tree.cli \
  --reindex --workspace data/workspaces/default \
  --clean datasets/*/full.md
#    --clean   重建前清空旧索引，防止新旧并存
#    --no-summary  纯结构重建，零 LLM 调用（快，但无节点摘要/文档描述）

# 3. 构建全文检索（BM25）索引
uv run python -m nianlun.indexing.fts.cli \
  --workspace data/workspaces/default --knowledge-base-id default

# 4. 可选：构建语义向量索引（需要 Embedding API）
uv run python -m nianlun.indexing.vector.cli \
  --workspace data/workspaces/default --knowledge-base-id default

# 5. 问答
uv run python -m nianlun.agent.cli               # 交互模式，流式输出
uv run python -m nianlun.agent.cli --no-stream   # 交互模式，整轮完成后一次性输出
```

各命令的完整选项可用 `--help` 查看。

## 目录

```text
nianlun/                  Python 包
datasets/                 开源数据集（一文一目录，见 datasets/README.md）
data/                     运行时数据：工作区索引、SQLite、上传产物（不提交）
evals/datasets/           批量问答问题集
evals/results/            批量运行结果
app/api_server/           FastAPI HTTP 服务层
app/frontend/             Web 前端（React + Vite）
```

接口字段以 API Server 运行时的 OpenAPI 文档（`/docs`）为准。

## 致谢与许可证

Nianlun 的结构化文档索引树思路及部分早期实现源自
[PageIndex](https://github.com/VectifyAI/PageIndex)，并在此基础上进行了独立重构与扩展。

Nianlun 的 Agent middleware、上下文管理、问题澄清与子 Agent 隔离设计参考了
[DeerFlow](https://github.com/bytedance/deer-flow)

Nianlun 采用 [MIT License](LICENSE) 开源。第三方项目的来源、使用范围及版权信息见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
