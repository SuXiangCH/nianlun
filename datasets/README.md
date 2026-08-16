# 数据集

本目录的财报数据集整理自 Datawhale AI 夏令营 2025「让 AI 读懂财报 PDF」公开数据集
（[ModelScope](https://www.modelscope.cn/datasets/Datawhale/AISumerCamp_multiModal_RAG)）：
在其 PDF 基础上经 MinerU 转换为 Markdown（`<doc-name>/full.md`），并由本项目
构建了标题树索引（见下文「预建树索引」）。再分发时遵循原数据集的许可协议。

## 目录格式

一文一目录，目录名即文档名，正文为目录下的 `full.md`（MinerU 等上游转换器的标准产物）：

```text
datasets/
  <doc-name>/
    full.md
```

只随数据集分发 `full.md` 正文；PDF 原件、版面模型输出与图片等转换中间产物
未随附，`full.md` 内的图片引用因此不可解析。检索与问答链路只消费文本，
不受影响。

## 预建树索引（workspace/）

`workspace/` 内含与上述数据集一一对应的已建树索引（`_meta.json` + 每文档一个
`<doc_id>.json`），**已包含 LLM 生成的节点摘要与文档描述**，可直接复用，无需再调
用模型重建。复制为本地默认工作区后，只需构建 FTS 索引即可开始问答：

```bash
mkdir -p data/workspaces
cp -r datasets/workspace data/workspaces/default

# FTS（BM25）索引：零 LLM、本地构建
uv run python -m nianlun.indexing.fts.cli \
  --workspace data/workspaces/default --knowledge-base-id default
```

树内 `path` 字段为指向 `datasets/<doc-name>/full.md` 的相对路径，仅作溯源元数据；
检索与问答只读取树 JSON 自身的文本，不依赖该路径。

## 导入 Web 服务（API Server）

部署了 Web 服务后，无需在前端逐个上传，一条命令把数据集连同预建树导入知识库，
前端即可直接展示：

```bash
uv run python -m app.api_server.import_datasets --knowledge-base 财报数据集 --create
```

说明：

- Markdown 正文与预建树一起走正常入库生命周期（SQLite 记录、workspace、
  content_version），**零模型调用**；重复执行按内容哈希幂等跳过。
- `--knowledge-base` 接受已有知识库的 id 或名称；`--create` 表示不存在时按该名称新建。
- 导入完成后自动触发 FTS 构建（本地 BM25，零模型调用，需 Milvus 可达）并等待完成。
- 建议在 API Server 停止或空闲时执行，避免并发写同一工作区。

## 从数据集构建索引

如需从 `full.md` 重新建树（例如更换摘要模型、调整索引参数）：

```bash
# 标题树索引（写入运行时工作区 data/workspaces/default/，该目录不提交）
uv run python -m nianlun.indexing.tree.cli \
  --reindex --workspace data/workspaces/default \
  --clean datasets/*/full.md

# 全文检索（BM25）索引
uv run python -m nianlun.indexing.fts.cli \
  --workspace data/workspaces/default --knowledge-base-id default
```

Web 端用户不需要本目录也可以工作：在界面创建知识库并上传文档即可，
入库与索引由 API Server 自动完成。
