# 文档分阶段处理流水线设计

> 状态：核心四阶段与超长 PDF 完整入库链路已落地；剩余限制见 §1.1
> 日期：2026-09-05
> 涉及模块：`app/api_server/services/documents/`、`app/api_server/services/document_ingestion_service.py`、`app/api_server/repositories/metadata/`、`app/api_server/database/`、`nianlun/indexing/tree/`、`app/frontend/src/features/knowledge-bases/`
> 关联文档：[MinerU 文档入库](mineru_document_ingestion_design.md)、[标题树索引](tree_index_design.md)、[全文检索](fts_design.md)、[向量检索](vector_index_design.md)

## 1. 结论

Nianlun 的文档处理应拆为四个可观察、可恢复的阶段：

```text
解析 parse -> 规范化 normalize -> LLM 增强 enrich -> 索引 index
```

PDF 超过 MinerU 单任务页数限制时，由解析阶段把原始 PDF 物理切成不超过 180 页的分段，
分别提交 MinerU；规范化阶段再按原始页序合并 Markdown、资源和页码映射。对用户、知识库、
索引和 Agent 来说，最终仍然是一份文档。

每个阶段都必须具备持久化状态、明确输入输出、幂等提交和局部重试。耗时计算不持有
`workspace_lock`；只有生成并发布不可变 workspace snapshot、推进知识库内容版本时短暂加锁。模型调用采用
文档级 worker 与进程级共享限流双层控制，使同一知识库的多份文档可以并行增强，同时不会
把模型服务压垮。

首版继续使用 SQLite 和进程内 executor，不引入 Redis、Celery 或新的基础设施。SQLite 保存
任务事实和租约，executor 只是执行载体；服务重启后可从数据库重新领取未完成任务。

### 1.1 当前实现状态

截至 2026-09-05，以下能力已经落地并有自动化测试覆盖：

- parse、normalize、enrich 持久化队列，带 lease token、过期 fencing、失败阶段重试和启动恢复；
- PDF 默认按 180 页物理分段，401 页 PDF 可作为同一文档完成解析、合并、发布和检索；
- 带空密码或仅权限限制的 PDF 在本地解密后重写为无加密 staging 文件；确实需要密码的 PDF 在
  parse preparation 阶段返回可操作错误；
- LLM 进程级总并发与单文档并发限制，默认分别为 8 和 4，并缓存 generation 内成功调用；
- LLM 单次调用使用覆盖 SDK 重试全过程的硬超时；阶段租约由独立 heartbeat 续期，worker 定时扫描
  并重新领取过期任务，模型请求卡住不会让任务永久停留在 `running`；
- 页、段、节点和索引文档进度，API 明确子模型与前端阶段状态展示；
- normalize 使用 Markdown/HTML parser 识别本地资源引用，将资源复制到 chunk 隔离目录并改写路径；
  `content_list.json` 的 block、页号和 Markdown 锚点均可无歧义验证时生成 `exact` 页映射，否则严格
  降级为 `chunk` 或 `unavailable`；
- immutable workspace revision、snapshot handle/refcount、原子发布、删除 tombstone、staging 对账与
  generation/snapshot GC；
- FTS/vector 增量更新、全量蓝绿 candidate collection、revision 原子切换、orphan 清理、状态收敛，
  以及 FTS 不可用时的同 revision 本地扫描降级。

仍存在以下已知限制，不能视为本设计已经完整验收：

- LLM 活跃数、等待数和阶段耗时尚未导出为指标；现有进度和任务表只能用于状态诊断；
- 节点摘要单点失败当前以 `partial` 和 warning 发布，与 §9.4 提出的“默认整阶段失败”策略仍有差异，
  后续需要结合产品容错要求统一；
- 部署仍限定单 API 进程；多进程共享目录前必须把进程内 snapshot reader 引用升级为跨进程 lease。

## 2. 改造前背景与问题

当前入库链路已经具备 MinerU 任务持久化、服务重启恢复、FTS/向量后台构建和知识库版本闸门，
但阶段边界没有真正建立：

```text
MinerU 完成
  -> 下载并解压 ZIP
  -> 建树
  -> 标题恢复
  -> 节点摘要与文档描述
  -> 写 workspace
  -> documents.status 由 parsing 直接变为 ready
  -> 调度知识库级 FTS / 向量索引
```

具体问题如下：

1. `documents.status` 虽然允许 `parsed` 和 `indexing`，当前主路径没有写入这两个状态；
   `parsing -> ready` 掩盖了 LLM 增强和索引等待时间。
2. LLM 增强嵌在 MinerU 结果持久化流程中，没有独立任务、错误状态和重试边界。
3. `workspace_lock` 覆盖了完整的建树和模型调用。同一知识库的多份文档即使已由不同轮询线程
   收到 MinerU 结果，也会在工作区锁外串行等待。
4. 单文档节点摘要虽然设置了 8 并发，但短节点不会调用模型，且文档间被工作区锁串行；模型侧
   经常只能观察到一个活跃请求。
5. PDF 作为一个完整文件提交 MinerU，无法处理上游单任务 200 页限制。已有 `page_ranges`
   是解析器 profile 的全局过滤配置，不是自动分段机制。
6. FTS 和向量索引有独立后台状态，但状态归属于知识库；文档详情无法说明当前在等待哪一种索引。
7. 当前轮询线程在任务未完成时 `sleep`，长任务会持续占用线程，不适合分段后成倍增加的远端任务量。

## 3. 目标与非目标

### 3.1 目标

- 支持超过 200 页的 PDF，默认按最多 180 页物理分段并自动合并；
- 一份源文件始终对应一个用户可见的 `document_id`，不把分段暴露成多份知识库文档；
- 解析、规范化、LLM 增强和索引阶段均可观察、可恢复、可局部重试；
- 同一知识库内允许多文档并行执行耗时的解析和 LLM 增强；
- 对模型请求设置明确的文档内并发和进程级总并发；
- 保留源文件、MinerU 结果和页码来源映射，维持 Agent 证据可追溯性；
- 保留现有 FTS/向量的知识库级合批、增量脏文档和 revision 闸门语义；
- 保持 MinerU SaaS 与私有部署两种模式，并保留私有任务丢失后的重提交能力；
- 不因 FTS 暂时不可用而伪装索引成功，保留本地扫描降级能力并明确展示警告。

### 3.2 非目标

- 不尝试修改或绕过 MinerU 服务端的页数限制；
- 不在首版引入 Redis、Celery、Kafka 或独立 worker 部署；
- 不把同一 PDF 的分段作为独立文档展示或独立参与检索；
- 不在首版实现跨机器的全局模型 QPS 精确控制；
- 不改变 Agent 工具现有的 `doc_id`、`node_id`、`line_spec` 和字符偏移语义；
- 不把 FTS、向量索引变成不可重建的事实来源。

## 4. 核心原则

### 4.1 阶段输入不可变

每个阶段读取已经发布的不可变输入，并把结果写到本次 pipeline generation 专属的 staging
目录。后续阶段只消费前一阶段成功发布的产物，不读取尚在写入的文件。

### 4.2 计算在锁外，发布时短暂加锁

PDF 拆分、ZIP 下载与解压、Markdown 合并、标题恢复、节点摘要、文档描述以及索引构建都不应
长期持有 `workspace_lock`。工作区锁只保护以下短操作：

- 校验文档仍处于同一个 pipeline generation；
- 原子发布目标 content revision 的 snapshot 目录；
- 更新根目录 legacy workspace 投影；
- 推进知识库 `content_version`。

### 4.3 状态与执行器解耦

SQLite 中的任务记录是事实来源。线程池中的 `Future` 只表示当前进程正在执行；服务重启、线程
取消或多进程竞争不能导致任务事实丢失。

### 4.4 至少一次执行，幂等提交

worker 可能因租约过期或进程崩溃重复执行，所以所有阶段按“至少一次”设计。外部副作用通过
稳定任务标识、输入指纹和 generation compare-and-swap 避免重复发布。

### 4.5 失败必须可观察

模型节点调用、标题恢复回退和索引降级不能只写空字符串或静默成功。允许兼容性回退时，需要
记录 `warning_code`、失败节点数和采用的 fallback；核心产物不完整时阶段必须失败。

## 5. 总体流程

```text
上传源文件
   |
   v
[parse]
   PDF/Word -> MinerU 分段任务 -> 分段 ZIP/Markdown
   Markdown -> 跳过远端解析，直接登记规范化输入
   |
   v
[normalize]
   分段排序、资源隔离、链接改写、Markdown 合并、页码映射
   |
   v
[enrich]
   标题恢复 -> 建树 -> 节点摘要 -> 文档描述
   |
   v
[publish]
   短锁 + generation 校验 -> 发布 full.md/tree JSON -> content_version + 1
   |
   v
[index]
   知识库级 FTS 增量构建 + 可选向量构建 -> revision 闸门
   |
   v
ready
```

`publish` 是 enrich 阶段的提交动作，不单独占用耗时 worker，但在事件记录和日志中作为明确步骤
展示。这样既保留阶段边界，也避免为了一个短事务引入额外任务表。

上传接口在源文件和首个任务记录持久化后即返回。Markdown 也改为后台 normalize/enrich，不再让
HTTP 请求同步等待全部 summary 调用；批量上传只负责逐文件接收，不负责串行完成整批增强。

## 6. 文档状态机

### 6.1 对外状态

沿用现有 `documents.status` 枚举，补齐实际状态转换：

| `documents.status` | 含义 | `current_stage` 示例 |
| --- | --- | --- |
| `uploaded` | 源文件已持久化，等待处理 | `parse` |
| `parsing` | 远端解析或本地规范化进行中 | `parse`、`normalize` |
| `parsed` | 规范化 Markdown 已完成，正在或等待增强 | `enrich` |
| `indexing` | 增强产物已发布，等待启用的索引追上版本 | `index` |
| `ready` | 内容可用，索引达到要求或已记录明确降级 | `complete` |
| `failed` | 核心阶段失败，需要重试 | `parse`、`normalize`、`enrich` |
| `deleted` | 已删除或等待后台清理 | `complete` |

新增字段：

```text
documents.pipeline_generation  INTEGER NOT NULL DEFAULT 1
documents.current_stage        TEXT NOT NULL DEFAULT 'complete'
documents.stage_state          TEXT NOT NULL DEFAULT 'succeeded'
documents.failed_stage         TEXT NULL
documents.progress_completed   INTEGER NULL
documents.progress_total       INTEGER NULL
documents.progress_unit        TEXT NULL       # pages/chunks/nodes/documents
documents.warning_json         TEXT NOT NULL DEFAULT '[]'
documents.deleted_content_version INTEGER NULL # 删除 tombstone 发布所在 revision
```

`current_stage` 取 `parse|normalize|enrich|index|complete`，`stage_state` 取
`queued|running|succeeded|partial|failed|skipped`。API 使用 Pydantic 模型解析 `warning_json`，不把松散字典
直接传播到 service 和前端。

`pipeline_generation` 在“使用最新配置重新处理”、替换源文件或管理员强制全流程重跑时递增。
普通失败重试保持 generation 不变，只增加对应任务的 attempt。

### 6.2 阶段转换

```text
uploaded/parse.queued
  -> parsing/parse.running
  -> parsing/normalize.running
  -> parsed/enrich.queued
  -> parsed/enrich.running
  -> indexing/index.queued
  -> indexing/index.running
  -> ready/complete.succeeded
```

核心阶段失败统一写 `status=failed`、`stage_state=failed`、`failed_stage=<stage>`。重试接口根据
`failed_stage` 恢复最小必要阶段：

- `parse`：只重试失败分段，保留成功分段；
- 迁移遗留的未分段 PDF 或分段任务集合不完整时：推进 `pipeline_generation`，从原文件重新执行
  parse preparation，并按当前分段策略创建任务；旧 generation 仅保留作审计；
- `normalize`：复用全部成功的分段结果重新合并；
- `enrich`：复用规范化 Markdown，重新生成增强结果；
- 索引警告：不重跑 MinerU 或 LLM，只重新调度对应知识库索引。

FTS 或向量失败不把已经成功发布的文档伪装成“解析失败”。确定采用以下策略：索引运行期间文档为
`status=indexing`；索引失败后文档转为 `status=ready`，同时在 `warning_json` 写入失败的索引类型和
知识库 revision。知识库级 `fts_status/vector_status` 保持 `failed`，Milvus 构建失败绝不能写成索引
成功。FTS 失败时按 §7.5 使用 committed snapshot 本地扫描；向量失败时只关闭可选语义路由。用户
重试索引后清除对应 warning，文档重新进入 `indexing`。

文档是否可以从 `indexing` 进入 `ready`，只检查该知识库实际启用的索引类型。某类索引配置为
`disabled` 时按 `skipped` 处理，不产生 warning，也不阻塞文档；两类索引都关闭时，发布事务直接写
`ready/complete.succeeded`。至少一类索引启用时，发布后先进入 `indexing`，reconciler 按以下规则收敛：

- 所有启用索引都已追上 `parsed_content_version`：进入 `ready/complete.succeeded`；
- 某索引仍为 pending/building：保持 `indexing/index.running`，即使另一索引已经失败；
- 所有启用索引均已终止，且至少一个在目标 revision 失败：进入 `ready/complete.partial` 并保留 warning；
- 重试失败索引时只清除该类型旧 warning，并重新进入 `indexing`；未重试类型的 warning 继续保留。

## 7. 持久化模型

### 7.1 扩展解析任务

保留 `document_parse_tasks`，将其从“每文档一次尝试”扩展为“每 generation、每分段的一次尝试”：

```text
pipeline_generation  INTEGER NOT NULL
chunk_index          INTEGER NOT NULL          # 从 0 开始
chunk_count          INTEGER NOT NULL
source_page_start    INTEGER NULL              # 原始 PDF，1 基、闭区间
source_page_end      INTEGER NULL
chunk_source_relpath TEXT NULL                 # 物理分段 PDF
result_root_relpath  TEXT NULL                 # 独立解压目录
input_sha256         TEXT NOT NULL
output_sha256        TEXT NULL
next_poll_at         TEXT NULL
dispatch_state       TEXT NOT NULL          # queued/leased/waiting/succeeded/failed/canceled
available_at         TEXT NOT NULL
lease_owner          TEXT NULL
lease_expires_at     TEXT NULL
lease_token          TEXT NULL              # 每次领取生成的新 UUID，作为 fencing token
```

现有 `state` 保留为 MinerU 业务状态（`created/uploading/waiting-file/pending/running/converting/done/failed`），
不得再用它表示本地 worker 是否正在执行。`dispatch_state` 表示本地提交/轮询队列状态：新任务为
`queued`，提交成功等待上游时为 `waiting`，到达 `next_poll_at` 后短暂变为 `leased`，远端完成后为
`succeeded`。这样 MinerU 的 `running` 不会与 SQLite 租约的 `leased` 混为一谈。

唯一约束改为：

```text
UNIQUE(document_id, pipeline_generation, chunk_index, attempt)
UNIQUE(provider, data_id, pipeline_generation, chunk_index, attempt)
```

任务提交时继续快照保存 `api_mode`、`model_version` 和 `request_json`，避免运行中修改默认 parser
改变既有任务协议。`data_id` 应包含 generation 和 chunk，例如：

```text
<document_id>:g2:c0003
```

### 7.2 新增规范化任务

新增 `document_normalization_tasks`，每个 document generation 至多一个成功任务：

```text
id, document_id, pipeline_generation, attempt
state                       # queued/running/succeeded/failed/canceled
available_at
input_manifest_json         # 有界的分段 ID、hash、页码范围列表
normalized_markdown_relpath
page_map_relpath
output_sha256
lease_owner, lease_expires_at, lease_token
error_code, error_message
created_at, updated_at, started_at, completed_at
```

`input_manifest_json` 由 Pydantic `NormalizationInput` 读写，限制最大分段数并验证连续、无重叠、
hash 完整，不能由调用方传入任意 JSON。

表上建立 `UNIQUE(document_id, pipeline_generation, attempt)`，并建立仅覆盖
`state IN ('queued','running','succeeded')` 的 partial unique index `(document_id, pipeline_generation)`。
分段聚合器在事务中选择每个 chunk 的成功 attempt，并用 `INSERT OR IGNORE` 创建 normalize 任务，
确保并发完成最后几个分段时不会重复进入下一阶段。

### 7.3 新增增强任务

新增 `document_enrichment_tasks`：

```text
id, document_id, pipeline_generation, attempt
state                       # queued/running/succeeded/partial/failed/canceled
available_at
input_sha256
llm_profile_id
llm_profile_updated_at      # 模型配置指纹的一部分
prompt_schema_version
options_json                # summary/heading recovery/tree build 快照
node_count, nodes_completed, nodes_failed
output_markdown_relpath
output_tree_relpath
output_sha256
lease_owner, lease_expires_at, lease_token
warning_json
error_code, error_message
created_at, updated_at, started_at, completed_at
```

增强重试默认使用原任务配置快照，保证同一次 pipeline generation 的可重复性。“使用当前模型和
配置重新处理”是另一个显式操作，会增加 `pipeline_generation`。

增强表使用与 normalize 相同的 attempt 唯一约束和 active/success partial unique index，保证阶段推进
幂等。`partial` 是已经发布 warning 的终态，也包含在 partial unique index 中。

为避免大文档因一个节点失败而重做全部模型调用，新增 `document_enrichment_call_results`：

```text
id, document_id, pipeline_generation
purpose                    # heading_recovery/node_summary/doc_description
unit_key                   # 稳定行范围或文档级固定键
input_sha256
model_fingerprint
prompt_schema_version
state                      # succeeded/failed
output_text                # 仅成功结果可复用
error_code
created_at, updated_at
UNIQUE(document_id, pipeline_generation, purpose, unit_key,
       input_sha256, model_fingerprint, prompt_schema_version)
```

`unit_key` 不依赖增强后可能变化的 `node_id`；节点摘要使用规范化 Markdown 的原始行范围和正文 hash，
文档描述使用 `document`。重试只复用 hash、模型指纹和 prompt 版本完全相同的成功结果；失败记录用于
诊断但必须重新调用。表不保存完整 prompt，输出与其他文档派生产物使用相同的数据保护和 generation
保留期。

### 7.4 索引状态

索引继续使用现有字段：

- 文档级 `fts_indexed_version`、`vector_indexed_version` 表示脏/已处理；
- 知识库级 `fts_status/vector_status` 表示任务状态；
- 知识库级 `content_version` 与 `fts_revision/vector_revision` 构成发布闸门。

不为每份文档创建 Milvus 构建任务。索引服务按知识库合并多个刚发布文档，避免每次上传都重建或
争用同一个 collection。索引完成或失败后调用文档状态 reconciler，把相应文档从 `indexing`
推进到 `ready`，或保留索引失败信息。

现有 `list_fts_dirty_documents` 和 `list_vector_dirty_documents` 只选择 `status='ready'`，实施时必须把
workset 拆成两类：upsert 选择已经成功发布且状态为 `indexing` 或 `ready` 的文档，并继续排除
`uploaded/parsing/parsed/failed`；tombstone 选择 `status='deleted'` 且对应 `*_indexed_version` 尚未达到
`deleted_content_version` 的文档。判断 upsert “已经发布”不能只依赖状态，还要校验
`parsed_content_version` 和当前 generation 的 tree artifact 均存在。builder 必须先完成 tombstone 的
delete-by-doc 并 flush，才能把该文档 ID 纳入成功结果；否则 finish 不得推进它的 indexed version。

索引 builder 不再直接写 `*_indexed_version`。它只返回本次成功处理的文档 ID；service 随后在一个
SQLite 事务中执行 `finish_*_build_and_mark_documents`：先校验知识库 `content_version`、目标 revision
以及向量模型指纹，校验成功后同时推进知识库 revision 并写入这些文档的 `*_indexed_version`。
校验失败时两者都不推进，相关文档保持脏状态并调度下一轮。因此文档级索引完成条件仍可使用
`*_indexed_version >= parsed_content_version`，但该字段只代表已经通过知识库发布闸门的结果，不能
代表尚未提交的 candidate build。

索引 pending/building/failed 时，新创建的 runtime 按 §7.5 使用固定 V2 snapshot 本地扫描，不把正在
增量修改的 collection 暴露给新请求。FTS 和同模型向量更新继续在当前 collection 上幂等刷新 dirty
文档；只有 finish 闸门成功后才把知识库置 ready 并写文档 indexed version。finish 失败时 dirty 标记
不推进，下一轮会再次对同一文档执行 delete-by-doc + insert，因此迟到或重复执行仍可收敛。

首次构建、强制重建、FTS schema 变化和向量模型指纹变化继续走蓝绿物理 collection：先构建新
collection，finish 成功时在 SQLite 中更新 collection 指针；失败或 revision 过期时保留旧指针并把
新 collection 作为 orphan 延迟清理。已经开始且持有旧 runtime 的单次请求可能在增量刷新窗口看到
collection 的短暂变化，这是保留现有增量写入的已知边界；新请求会因 capability fingerprint 变化
切换到 snapshot 本地扫描。若未来要求在途请求也具备严格 snapshot isolation，需要另行采用全量蓝绿
或可按 document generation 过滤的索引 schema。

### 7.5 FTS 本地降级

当前 Web runtime 强制创建 Milvus `FullTextNodeRetriever`，collection 缺失或 schema 过期会直接失败；
因此“文档 ready 后允许本地扫描”需要作为本设计的一部分实现，不能只改状态文案。

新增 `LocalScanNodeRetriever`，从 API 固定的 V2 snapshot 读取 tree artifact，实现与
`FullTextNodeRetriever.search(query, limit, doc_ids)` 相同的稳定端口和返回字段。它使用现有 FTS record
生成规则进行有界本地匹配，保留 `doc_id/node_id/line_num/source_type` 和截断信息，不连接 Milvus。
本地扫描只作为不可用降级，不得把结果或知识库 `fts_status` 标为远端索引构建成功。

`KnowledgeBaseFactory` 的选择规则固定为：

- 当前 `fts_status=ready`、`fts_revision=content_version` 且 collection schema 有效：使用 Milvus；
- 其他状态但 committed snapshot 有效：使用 `LocalScanNodeRetriever`，记录结构化 warning；
- committed snapshot 无效：拒绝创建 runtime。

应用创建不再以 FTS ready 作为绑定前置条件，只要求知识库存在有效 committed snapshot；运行时根据
上述规则选择检索器。runtime capability fingerprint 已包含 FTS 状态和 revision，因此 FTS 恢复后会
自动从本地扫描切回 Milvus。向量仍为可选能力，失败时不注册语义文档工具。

### 7.6 产物类型

`document_artifacts.kind` 的约束新增以下类型，并同步迁移 ORM、repository 和 API literal：

```text
parse_chunk_source
parse_chunk_result
normalized_markdown
page_map
enriched_markdown
tree
diagnostics
```

兼容期继续识别现有 `result_zip/full_markdown/content_list/asset`。新 generation 产物必须记录
`pipeline_generation` 和 `sha256`；因此 `document_artifacts` 新增
`pipeline_generation INTEGER NOT NULL DEFAULT 1`。仅靠 relpath 中的 generation 不足以支撑
repository 查询和 GC。

### 7.7 Workspace revision

新增 `knowledge_base_workspace_revisions`：

```text
knowledge_base_id
content_version
snapshot_relpath
manifest_sha256
state                   # committed/superseded
created_at
UNIQUE(knowledge_base_id, content_version)
```

它记录 API runtime、FTS 和向量索引实际读取的不可变 workspace snapshot。snapshot manifest 使用
明确的 V2 Pydantic 契约，至少包含 `schema_version=2`、`knowledge_base_id`、`content_version`，以及
每个文档的 `document_id/index_relpath/content_relpath/page_map_relpath/generation`，以及有界的
`artifact_files[] {relpath, size_bytes, sha256}`。所有相对路径必须解析在 workspace 内；manifest 对文档
数、单文档文件数和序列化大小均设上限。启动 reconciliation 和 reader 打开 snapshot 时校验 manifest
hash；文件内容按首次打开或构建前校验，不能仅因 manifest 自身完整就信任被引用 artifact。

创建知识库时即生成并校验空的 `snapshots/r0/manifest.json`，再在同一数据库事务中插入
`content_version=0` 的 committed revision；后续首次文档发布和空知识库 runtime 都沿用同一读取协议，
不为 revision 0 创建特殊分支。新 revision 提交时把前一条 revision 标为 `superseded`；该状态只表示
不再是当前 revision，不表示文件可以立即删除。

`nianlun.knowledgebase`、FTS 和向量 builder 新增读取 V2 snapshot 的入口；API Server 必须传入 SQLite
中当前 `content_version` 对应的 snapshot。现有根目录 `_meta.json + <doc_id>.json` 继续作为 V1 离线
CLI 兼容投影，V1 reader 保持不变，但 API 在线读取、索引构建和一致性判断不得再依赖该投影。

页码 provenance 首版只保存在 V2 snapshot 和内部检索记录中，不改变 Agent 工具的请求或返回 schema，
因此不提升 `AGENT_TOOL_SCHEMA_VERSION`。如果后续把 `page_number/page_range/page_precision` 加入工具
响应，必须同步更新 `nianlun/agent/contracts.py`、主/子 Agent 边界、前端引用类型和相关测试，并评估
提升该版本常量。

### 7.8 阶段事件

新增有界的 `document_pipeline_events` 用于 UI 历史和问题定位：

```text
id, document_id, pipeline_generation, stage, event_type
progress_completed, progress_total, message, created_at
```

事件只保存状态和脱敏消息，不保存 API Key、Authorization header、完整模型 prompt 或文档正文。
高频进度按阶段覆盖 `documents` 当前值，仅阶段转换和错误追加事件。每个 document generation 最多
保留 100 条事件，超过后优先删除最旧的普通进度事件，失败和发布事件随 generation 保留期清理，
避免 SQLite 无限增长。

### 7.9 Staging 配额记账

仅检查单个 ZIP 大小不能限制并行分段的累计占用。新增 `document_staging_allocations`：

```text
document_id, pipeline_generation, allocation_key
kind                       # chunk_pdf/result_zip/extracted/temp_output
bytes_reserved
state                      # active/released
updated_at
UNIQUE(document_id, pipeline_generation, allocation_key)
```

流式写入每增加一批字节，都在 `BEGIN IMMEDIATE` 短事务中累计本 generation 的 active allocation；只有
`SUM(bytes_reserved) + delta <= document_staging_max_bytes` 时才允许继续写入。ZIP entry 在解压前按声明
大小预留，写完后按实际大小校正；异常清理时释放对应 allocation。进程崩溃可能造成保守的过量记账，
但不能造成少记；启动 reconciliation 根据受控 staging 目录中的实际文件重建 allocation，无法确认归属
的文件计入隔离态 allocation，并按 orphan 保留期处理，避免在清理前形成未记账空间。配额表只记录
大小和路径无关的稳定 allocation key，不记录文档内容。

## 8. 超长 PDF 分段

### 8.1 预检与拆分

引入 `pypdf`，只在上传后的后台解析阶段读取 PDF 页数并拆分，不在 HTTP route 内执行长期阻塞
工作。默认参数：

```text
pdf_chunk_max_pages = 180
pdf_chunk_overlap_pages = 0
pdf_max_chunks = 100
document_staging_max_bytes = 2 GiB
```

180 页为 MinerU 200 页限制保留余量。默认不设置重叠页，因为重叠会造成正文、表格和图片重复，
可靠去重比页边界上下文收益更难保证。如真实语料验证存在明显跨页质量损失，再单独设计带页级
来源的重叠去重。

对于用户显式配置的 `page_ranges`：

1. 先规范化并与 PDF 总页数求交集；
2. 将选中页按连续区间分组；
3. 每个区间再切成不超过 `pdf_chunk_max_pages` 的物理 PDF；
4. 记录每个输出页对应的原始页码。

物理拆分优先于只向 MinerU 传 `page_ranges`，因为部分服务会按上传文件总页数拒绝请求，即使请求
只选择其中少量页。分段文件写入：

```text
staging/<document_id>/g<generation>/parse/chunks/0000.pdf
```

源 PDF 始终保留在 `sources/`。分段 PDF 是可重建派生产物，可在整个 generation 成功并经过保留期
后清理；失败或等待重试时不得删除。

PDF 带加密标记时先尝试空密码解密。空密码或仅权限限制的文件解密成功后，即使没有超过分段页数，
也必须经 `PdfWriter` 重写为无加密 staging PDF 再提交 MinerU；确实需要打开密码、加密算法不受支持、
文件损坏或零页时，parse 阶段以可操作错误失败，不尝试提交 MinerU。拆分过程逐页写临时文件并在
完成后原子改名，不能在内存中同时构造全部分段。

### 8.2 分段调度

每个分段是独立 MinerU 任务，允许并行提交和轮询。一个分段失败不会删除其他已成功结果。文档
进度按原始页数聚合：

```text
progress_completed = 成功分段覆盖的原始页数
progress_total = 本次选择的总页数
progress_unit = pages
```

分段全部成功后才创建 normalize 任务。部分成功不能发布为完整文档，除非未来增加用户明确确认的
“接受部分结果”操作；首版不做静默部分入库。

### 8.3 结果目录与资源冲突

每个 ZIP 解压到隔离目录：

```text
staging/<document_id>/g<generation>/parse/results/0000/
staging/<document_id>/g<generation>/parse/results/0001/
```

MinerU ZIP 必须流式下载到 claim-token 临时文件，分别限制单个压缩包大小、ZIP entry 数、单 entry
解压大小、压缩比和同一 document generation 的累计 staging 字节数；累计值通过 §7.9 的 allocation
事务预留。超过任一限制即让对应分段失败，不能把多个“各自小于上传上限”的分段累积成无界磁盘占用。

规范化阶段完成以下操作：

- 按 `chunk_index` 选择并读取每段主 Markdown；
- 将图片、表格和其他相对资源复制到 `chunks/<chunk_index>/assets/`，解析阶段输入在整个 normalize
  成功提交前保持不可变；
- 使用 Markdown token 和 HTML fragment parser 改写 Markdown link/image destination 及原始 HTML
  的 `src/href`，不做字符串全局替换；
- 按页序拼接 Markdown，段间只插入规范化换行，不插入影响标题树的伪标题；
- 合并 `content_list.json` 等结构化结果，并把分段页号映射回原始 PDF 页号；
- 生成 `page_map.json`，记录 Markdown 行范围、chunk 和原始页范围；
- 校验所有本地资源引用均落在当前 generation 目录内。

规范化输出为不可变的：

```text
artifacts/<document_id>/g<generation>/normalized/full.md
artifacts/<document_id>/g<generation>/normalized/page_map.json
artifacts/<document_id>/g<generation>/normalized/chunks/.../assets/
```

标题恢复只修改标题行的 `#` 数量，不应破坏行号和页码映射。若未来增强步骤会增加或删除行，必须
同步生成从增强 Markdown 到规范化 Markdown 的行号映射。

`content_list.json` 是可选上游产物，页码映射必须声明精度，不能根据 Markdown 内容猜测一个精确
页码。`page_map.json` 使用 Pydantic `PageMap`：

```text
precision: exact | chunk | unavailable
entries[]:
  markdown_line_start
  markdown_line_end
  chunk_index
  source_page_start
  source_page_end
  evidence: content_list | chunk_boundary
```

- 只有 `content_list` 提供页号，且其 block 能按顺序、无歧义地映射到 Markdown 行范围时，才写
  `precision=exact`；
- 无法建立 block 级映射但分段范围已知时，每段 Markdown 只映射到原始页范围，写
  `precision=chunk`；
- Word 或上游结果连分段范围都无法提供时写 `precision=unavailable` 和 warning；
- 映射必须单调、覆盖有效行且页码落在源范围内；验证失败时降级精度，不伪造页号。

现有 Agent 定位仍以 `line_spec` 和字符偏移为准。页码作为新增可选 provenance 字段向后兼容；只有
`precision=exact` 时才能展示单页引用，`chunk` 只能展示页范围。

## 9. LLM 增强阶段

### 9.1 阶段边界

增强阶段读取 `normalized/full.md`，在 staging 中完成：

1. 规则和可选 LLM 标题层级恢复；
2. Markdown 树构建；
3. 节点摘要；
4. 文档描述；
5. 输出校验和诊断记录。

成功输出：

```text
artifacts/<document_id>/g<generation>/enriched/full.md
artifacts/<document_id>/g<generation>/enriched/tree.json
artifacts/<document_id>/g<generation>/enriched/diagnostics.json
```

`summary_enabled=false` 时跳过节点摘要和文档描述，但仍经过独立增强任务完成规则建树；
`heading_recovery_enabled=false` 时保留来源标题层级。这样所有文档沿用同一阶段契约，而不是为开关
组合建立不同主流程。

### 9.2 并发控制

建议新增配置：

```text
NIANLUN_API_ENRICH_WORKERS=2
NIANLUN_API_LLM_MAX_CONCURRENT=8
NIANLUN_API_LLM_PER_DOCUMENT_CONCURRENT=4
NIANLUN_API_LLM_REQUEST_TIMEOUT_SECONDS=300
```

- `ENRICH_WORKERS` 限制同时处理的文档数；
- `LLM_PER_DOCUMENT_CONCURRENT` 防止单个大文档独占所有请求槽；
- `LLM_MAX_CONCURRENT` 是所有知识库、Markdown/PDF 入口共享的进程级上限；
- `LLM_REQUEST_TIMEOUT_SECONDS` 是一次模型调用覆盖 SDK 内部重试全过程的硬 deadline，超时会取消
  当前调用并释放并发槽；
- 实际并发为三个限制的交集，而不是简单相乘。

`EnrichmentService` 持有一个专用 asyncio event loop 和一个文档级调度协程；所有文档的 async tree
pipeline 和模型调用都在这个 loop 中执行。`ENRICH_WORKERS` 实际由 document semaphore 控制，不再为
每份文档调用 `build_md_index_sync` 创建独立 event loop。PDF/Markdown 解析等 CPU 或阻塞文件操作通过
专用 bounded executor 执行，再把纯数据结果交回 event loop。

`ModelCallLimiter` 在该 loop 内持有一个全局 `asyncio.Semaphore`，并为每个 document task 持有一个
per-document semaphore。tree summary、文档描述和标题恢复统一依赖包装后的模型端口，调用顺序固定为
先取得 document slot、再取得 global slot，`finally` 中逆序释放；取消任务也必须释放容量。这样没有
跨 event loop 共享 asyncio primitive，也不会用阻塞式 `threading.Semaphore.acquire` 占满 worker。
不得只在某一个 prompt 函数内限流，否则其他增强模型调用仍会绕过总预算。

多进程部署时每个进程各自持有一份容量，因此总上限约为 `进程数 * LLM_MAX_CONCURRENT`。首版在部署
文档中明确仅启动一个 API worker；以后需要多进程精确限流时再引入外部协调器。

### 9.3 公平性和模型侧队列

增强 service 以文档为调度单元，使用 FIFO 文档队列和每文档容量限制共享全局槽。默认两个文档各可
占最多 4 个槽，因此同一知识库的两份文档可以同时在模型侧出现请求，同时总请求不超过 8。调度器
不得在某份文档仍有 4 个在途请求时继续把它的其他节点放入全局 semaphore 等待队列，避免先入队的
大文档长期占据所有候选位置。

短于 `summary_token_threshold` 的节点仍可直接使用正文而不调用模型，但需要分别统计：

```text
nodes_total
nodes_model_requested
nodes_short_circuited
nodes_succeeded
nodes_failed
model_requests_active / model_requests_queued
```

这组指标可以解释“配置并发为 8，但模型侧只有一个任务”的情况，避免仅凭模型服务队列猜测本地
执行状态。

### 9.4 错误策略

- 429、超时和 5xx 按指数退避进行有界重试，尊重 `Retry-After`；
- 鉴权、模型不存在和输入超限视为非重试错误；
- 节点摘要最终失败必须累计到 `nodes_failed` 并记录脱敏错误码；
- 默认只要存在应调用模型但失败的节点，enrich 任务即失败，不发布带无声空摘要的结果；重试复用
  §7.3 已成功且指纹匹配的调用结果，只重新请求失败或未完成单元；
- 标题恢复允许既有的规则 fallback，但必须以 `partial` 和 warning 形式可观察；
- 如产品以后允许部分摘要，应作为显式 profile 策略，而不是异常捕获后的默认行为。

## 10. SQLite 队列与 worker

### 10.1 任务领取

各 stage repository 提供原子 `claim_next_*_task(worker_id, lease_seconds)`。领取在短事务中完成：

1. normalize/enrich 选择 `state=queued`，parse 选择 `dispatch_state IN (queued, waiting)`，同时要求
   到达 `available_at/next_poll_at` 且没有有效租约；
2. 生成不可复用的 `lease_token`，使用条件更新写入本地 leased/running 状态、`lease_owner`、
   `lease_token`、`lease_expires_at`；parse 的 MinerU `state` 不在领取时修改；
3. 返回包含 `lease_token` 的任务；
4. 未更新到行时说明任务已被其他 worker 领取。

长任务通过独立 heartbeat 定期续租，不依赖节点完成后才更新的业务进度。heartbeat 间隔不超过
30 秒且不超过租约时长的三分之一。续租、进度更新和最终提交都使用
`WHERE id=? AND pipeline_generation=? AND lease_token=? AND lease_expires_at>now` 条件更新。worker ID
只用于诊断，`lease_token` 才是 fencing token；即使进程重启复用了 worker ID，旧 worker 也无法提交。
过期 worker 的迟到结果写入自己的 claim-token staging 目录后丢弃，不得覆盖新租约或新 generation。

### 10.2 MinerU 轮询

远端任务等待期间不占用一个持续睡眠的线程。改为单次轮询：

```text
dispatch_state=waiting + next_poll_at 到期
  -> 领取短租约
  -> 请求一次上游状态
  -> 完成：持久化结果并进入后续阶段
  -> 未完成：更新 MinerU state/progress/next_poll_at，dispatch_state 回到 waiting，释放租约
  -> 失败：按错误策略重试或置 failed
```

建议默认并发：

| 执行类型 | 默认并发 | 说明 |
| --- | ---: | --- |
| MinerU 提交 | 2 | 控制上传带宽和上游突发流量 |
| MinerU 单次轮询 | 4 | 请求短，不长期占线程 |
| normalize | 2 | 本地 IO/CPU |
| enrich 文档 | 2 | 受共享模型限流进一步约束 |
| FTS 知识库 | 1 | 沿用当前默认 |
| vector 知识库 | 1 | 沿用当前默认 |

### 10.3 启动恢复

启动时按以下规则恢复：

- normalize/enrich 的 `queued` 和 parse 的 `dispatch_state=queued`：重新加入对应 executor；
- 本地 running/leased 且租约未过期：等待租约，不重复执行；
- 本地 running/leased 且租约过期：清除旧 `lease_token`，恢复 queued 或 waiting 并增加 recovery 计数；
- normalize/enrich worker 即使没有收到新任务 wake 事件，也按 heartbeat 周期扫描一次到期任务，避免
  启动检查时租约尚未过期的任务随后永久停留在 `running`；
- MinerU 已有 `batch_id/task_id`：恢复单次轮询，不重复提交；
- 私有 MinerU 返回任务不存在：沿用现有逻辑，从分段 PDF 重提交该分段；
- 阶段输出已存在且 hash 匹配：幂等确认成功并调度下一阶段；
- 输出存在但 hash 或 generation 不匹配：隔离为 orphan，记录告警并重新执行。

## 11. 发布与工作区并发

### 11.1 不可变 generation 与 revision snapshot

单文档增强产物按 generation 保存，知识库可见视图按 content revision 保存：

```text
artifacts/<document_id>/g3/enriched/full.md
artifacts/<document_id>/g3/enriched/tree.json
snapshots/r42/manifest.json
```

generation 解决“同一文档新处理结果不能覆盖旧结果”，revision snapshot 解决“知识库中的多份文档
必须组成一个一致视图”。`snapshots/r42/manifest.json` 只引用不可变 artifact，不复制正文和 tree，
因此创建新 revision 的成本与文档数量线性相关但数据量有界。旧 generation 和旧 snapshot 在新版本
发布前继续可读。

### 11.2 单向发布协议

SQLite 是在线服务的当前 revision 权威来源。增强计算完成后执行：

1. 在锁外把本 generation 的最终文件写入带 `lease_token` 的临时目录，校验 schema、资源引用和 hash；
2. 获取 `workspace_lock`，重新读取任务和文档，验证租约有效、文档未删除、generation 与输入 hash
   仍匹配；
3. 将任务临时目录原子重命名为不可变 `artifacts/<document_id>/g<generation>/enriched/`；目标已存在且
   hash 相同时视为幂等成功，hash 不同时拒绝覆盖并报告一致性错误；
4. 读取 SQLite 当前 `content_version=N` 对应的 committed snapshot，替换其中当前文档条目；
5. 生成并校验 `snapshots/.staging-<publish_id>/manifest.json`，目标 revision 固定为 `N+1`；
6. 原子重命名 snapshot 目录为 `snapshots/r<N+1>/`；
7. 在一个 SQLite 事务中再次校验任务 `lease_token` 和知识库 `content_version=N`，插入
   `knowledge_base_workspace_revisions(N+1)` 并将 revision N 标为 superseded，更新文档 artifact 指针和
   `parsed_content_version=N+1`，推进知识库 `content_version=N+1`，将启用的索引置 pending，并将
   本 generation 来自首次上传时对应的 `upload_operations` 从 `files_committed` 置为 `committed`；同时
   完成 enrichment task 并按 §6.2 把文档置为 `indexing`，或在索引均关闭时直接置为 `ready`；
8. 提交成功后释放锁，调度 FTS/向量索引；
9. 最后由异步任务另行短暂获取 `workspace_lock`，更新根目录 V1 legacy 投影，供离线 CLI 使用，不参与
   在线 revision 判定。

第 7 步是一次不可拆分的 repository 操作；不能先把 upload operation 标为 committed，再单独推进
revision。reprocess generation 没有对应 upload operation 时跳过该字段更新。发布事务因 revision CAS
失败时，upload operation 保持 `files_committed`，任务释放旧租约并基于最新 snapshot 重新发布；幂等上传
查询返回同一文档及其当前 pipeline 状态，不创建第二份文档。

legacy 投影任务携带目标 revision；取得锁后必须重新读取 SQLite，只有目标仍是当前
`content_version` 时才执行，并最后写 `_meta.json`。旧 revision 的迟到投影任务直接丢弃，不能把根目录
回退到旧内容。该投影只提供兼容期的 best-effort 离线视图，不承诺与在线请求并发时的 snapshot isolation。

这个顺序只有单向可见性：SQLite 指向新 revision 前，完整且校验通过的 snapshot 目录一定已经存在；
snapshot 目录提前存在但 SQLite 尚未引用时只是不可见 orphan。不存在“数据库已提交但 snapshot 尚未
生成”的正常路径。

服务启动时执行确定性 reconciliation：

- SQLite committed revision 对应目录和 hash 均有效：允许服务并修复落后的 V1 legacy 投影；
- committed revision 的目录缺失或 hash 不符：将知识库置 error，禁止构建 runtime，不能猜测补齐；
- snapshot 目录没有 SQLite revision 记录：视为 orphan，超过保留期后清理，不反向推进数据库；
- generation artifact 没有任何 committed snapshot 或活跃任务引用：超过保留期后清理；
- `.staging-*` 只在确认没有活跃租约且超过保留期后清理。

### 11.3 在线读取和索引快照

API 创建 Agent runtime 时，根据 SQLite `content_version` 打开对应 V2 snapshot；runtime capability
fingerprint 已包含 `content_version`，revision 推进后会重建并清空旧的文档缓存。若 snapshot 校验失败，
runtime 创建失败并暴露知识库错误，不回退到可能内容不同的根目录 V1 投影。

FTS/向量 service 同样从任务固定的 V2 snapshot 读取，不再扫描可变根目录，也不在构建期间持有
`workspace_lock`。snapshot 自身不可变，所以索引任务只需持有 `(knowledge_base_id, revision,
manifest_sha256)`；删除和新上传通过创建新 revision 生效，不会改变在途任务的输入。

完成时使用 §7.4 的原子 finish：只有知识库当前 `content_version` 仍等于目标 revision，且模型指纹
匹配，才推进知识库 revision 并标记本批文档 indexed；蓝绿构建同时切换 SQLite collection 指针。
否则文档保持脏状态并合并调度下一轮；只有蓝绿构建产生的新物理 collection 需要记为 orphan。

在线读者通过 `WorkspaceSnapshotHandle` 持有 snapshot，不直接保存裸路径。handle 在打开 manifest 前
向进程内 `WorkspaceSnapshotRegistry` 增加 `(knowledge_base_id, content_version)` 引用，runtime 缓存、
单次请求和索引 builder 在各自生命周期结束时释放；引用增减与 GC 候选删除由同一个 registry 锁串行化。
GC 只可删除同时满足以下条件的 superseded snapshot：

- 不是 SQLite 当前 `content_version`；
- 未被 pending/building 索引的目标 revision 引用；
- 进程内 reader refcount 为 0；
- 已超过配置的最短保留期，并至少保留最近两个已发布 revision。

generation artifact 只有在没有任何保留中的 snapshot、活跃 pipeline task 或 staging allocation 引用时
才可删除。删除文档只发布一个不再包含该文档的新 revision，不立即删除旧 snapshot 或 artifact。
首版限定单 API worker，因此进程内 refcount 足够覆盖所有在线读者；进程退出后不存在仍存活的 reader。
未来支持多 API/worker 进程前，必须把 reader 引用改为带过期时间的 SQLite lease 或由各进程独立保留
文件，不能直接沿用进程内 refcount 执行共享目录 GC。

## 12. API 与前端契约

### 12.1 文档响应

`DocumentResponse` 新增：

```json
{
  "status": "parsed",
  "current_stage": "enrich",
  "stage_state": "running",
  "pipeline_generation": 2,
  "published_content_version": 41,
  "progress": {
    "completed": 17,
    "total": 42,
    "unit": "nodes"
  },
  "failed_stage": null,
  "warnings": [],
  "parse": {
    "chunks_completed": 2,
    "chunks_total": 3,
    "pages_completed": 360,
    "pages_total": 520
  }
}
```

保持 snake_case，并由服务端和前端共享明确类型。旧 `latest_task` 在兼容期保留，但新增 UI 不再用
单个 latest parse task 推断整份长文档进度。`published_content_version` 是当前仍在线可见的 V2 snapshot
revision；新 generation 处理中或失败时，它仍指向旧 generation 所在 revision，尚未发布过则为 null。

`GET .../documents/{document_id}/pipeline` 的每个 parse chunk 同时返回 MinerU `state` 和本地
`dispatch_state`，但不返回 `lease_owner/lease_token`。列表响应只返回页数和分段数聚合，不把多个 chunk
压成一个含义不清的 `state`；前端阶段文案以文档 `current_stage/stage_state` 为准。

### 12.2 操作接口

- `POST .../documents/{document_id}/retry`：重试 `failed_stage` 的最小范围；
- `POST .../documents/{document_id}/reprocess`：使用当前 parser/LLM/树配置创建新 generation；
- `GET .../documents/{document_id}/pipeline`：返回有界阶段历史、分段聚合和错误；
- 删除接口先把 generation 及其 queued/running 任务标记 canceled，再执行索引删除和延迟文件清理。

请求取消仍只取消 HTTP 上传读取，不应取消已经确认接收并持久化的后台任务。后台任务如需取消，
必须通过显式文档删除或未来的 cancel endpoint。

### 12.3 前端展示

文档列表使用具体阶段文案：

- 等待解析、解析中 `360/520 页`；
- 正在合并 `2/3 段`；
- LLM 增强 `17/42 节`；
- 正在构建全文索引 / 向量索引；
- 可用；
- LLM 增强失败，可重试；
- 可用，但向量索引失败。

活跃任务期间前端定时刷新文档状态；首版不为低频后台进度新增 SSE 协议。查看内容按钮以已发布的
`parsed_markdown_relpath` 是否存在为准，不应因新 generation 正在重处理而隐藏仍可用的旧版本。

## 13. 配置

新增环境变量时同步更新 `.env.example` 和配置测试：

```text
NIANLUN_API_PDF_CHUNK_MAX_PAGES=180
NIANLUN_API_DOCUMENT_STAGING_MAX_BYTES=2147483648
NIANLUN_API_MINERU_SUBMIT_WORKERS=2
NIANLUN_API_MINERU_POLL_WORKERS=4
NIANLUN_API_NORMALIZE_WORKERS=2
NIANLUN_API_ENRICH_WORKERS=2
NIANLUN_API_LLM_MAX_CONCURRENT=8
NIANLUN_API_LLM_PER_DOCUMENT_CONCURRENT=4
NIANLUN_API_TASK_LEASE_SECONDS=120
NIANLUN_API_SNAPSHOT_MIN_RETENTION_SECONDS=86400
NIANLUN_API_STAGING_ORPHAN_RETENTION_SECONDS=86400
```

约束：

- `PDF_CHUNK_MAX_PAGES` 必须大于 0，默认值不得超过当前支持的 MinerU 上限；
- staging 上限必须覆盖至少一个合法上传文件，同时限制所有分段 ZIP 的累计压缩和解压数据；
- per-document LLM 并发不得超过全局并发；
- lease 必须大于单次上游请求超时，并由长任务续租；
- snapshot 和 staging orphan 保留期必须大于 0；GC 仍需同时满足 §11.3 的引用条件；
- 所有路径基于知识库 workspace 和注入 settings 解析，不依赖当前 shell 目录。

## 14. 一致性与删除

- 分段解析成功不推进 `content_version`，只有增强产物发布才推进；
- normalize/enrich staging 产物不进入 FTS 或向量索引；
- 新 generation 失败时，旧 generation 如果存在则继续服务，但 UI 显示重处理失败；
- 文档删除先在事务中把当前 generation 的任务置 canceled，使迟到 worker 的 fencing 校验失败；再按
  §11.2 创建一个不包含该文档的 V2 snapshot，并在发布事务中将文档置 deleted、推进
  `content_version` 和启用索引的 target revision，同时写入 `deleted_content_version`；后续索引 workset
  必须把该文档作为 tombstone 处理；
- 删除接口在新 revision 提交后即可返回，旧 snapshot、文档 generation artifact 和源文件只按 §11.3
  的引用与保留期规则延迟 GC；索引删除失败时保留 warning 和 dirty 状态，不回滚已经发布的内容删除；
- 任务迟到结果只能落入 staging，不得重新创建已删除文档；
- FTS/向量仍遵守“先写 Milvus 并 flush，后标记 indexed_version”；
- Parser、LLM 或 Embedding 配置快照不包含 API Key，只保存 profile ID 和非敏感指纹；
- 日志不记录完整 Markdown、模型 prompt、上传内容或签名下载 URL。

## 15. 数据库迁移

数据库变化必须通过 `database/migrations.py` 提供幂等向前迁移：

1. 为 `documents` 添加 pipeline、进度和 `deleted_content_version` 字段；
2. 为 `document_parse_tasks` 添加 generation、chunk、dispatch、available_at、lease token 与输出字段，
   同步重建 state/check/index 约束和 API literal；
3. 重建 SQLite 唯一约束，迁移旧任务为 `generation=1/chunk_index=0/chunk_count=1`，根据既有 MinerU
   state 确定初始 dispatch state；
4. 新建 normalization、enrichment、enrichment call result 和 event 表及领取/partial unique 索引；
5. 扩展 `document_artifacts.kind` 约束并添加 `pipeline_generation`；
6. 新建 `knowledge_base_workspace_revisions`，为每个现有 workspace 的当前 `content_version` 生成 revision
   snapshot 并校验 hash；新建知识库必须原子初始化空的 revision 0；
7. 新建 `document_staging_allocations` 及 generation/active 查询索引；迁移时不猜测历史临时文件用量，
   由首次启动 reconciliation 在 worker 开始领取任务前扫描受控 staging 目录；
8. 保留 `upload_operations` 现有状态集合，调整发布 repository，使首次上传的 operation 与 V2 revision
   在同一事务提交；
9. 将现有 `ready` 文档映射为 `complete/succeeded`；
10. 将现有 `parsing` 文档映射为 `parse/queued|running`，保留 batch/task ID；
11. 对历史 `parsed/indexing` 状态按现有 artifact 和 indexed version 做确定性 reconciliation；
12. 迁移结束后运行 schema 完整性检查并只记录新的 schema version。

升级测试必须从包含运行中 MinerU 任务、失败任务和 ready 文档的旧数据库 fixture 开始，验证迁移后
不会重复发布、丢失源文件或把失败任务误标成功；还要验证 snapshot/call result/staging allocation 表
可重复迁移，以及首次启动的 staging 记账重建不会超过实际占用或在 worker 启动后竞态少记。

## 16. 可观测性

建议结构化日志和指标至少覆盖：

```text
document_stage_duration_seconds{stage,outcome}
document_tasks_queued{stage}
document_task_recoveries_total{stage}
mineru_chunks_active / mineru_chunks_failed
mineru_pages_completed
llm_requests_active / llm_requests_queued
llm_requests_total{purpose,outcome}
llm_node_short_circuits_total
workspace_lock_hold_seconds{operation}
index_revision_lag{kind,knowledge_base_id}
```

日志关联字段包含 `request_id`、`knowledge_base_id`、`document_id`、`pipeline_generation`、`stage`、
`task_id` 和 `chunk_index`。外部 MinerU batch/task ID 可记录，签名 URL 和凭据不得记录。

验收时应能直接回答：

- 一份文档当前卡在哪个阶段；
- 520 页 PDF 已完成多少页、哪个分段失败；
- 模型并发槽当前用了多少、多少请求在本地等待；
- workspace 锁一次持有多久；
- 文档已发布但 FTS/向量落后多少 revision。

## 17. 测试策略

### 17.1 单元测试

- 用 5 页确定性 PDF fixture 和 `chunk_max_pages=2` 验证分段为 `1-2/3-4/5`；
- 显式 `page_ranges` 的交集、非连续区间、越界页和空选择；
- 分段资源重名、相对链接改写、路径穿越与 ZIP 大小限制；
- Markdown 合并顺序、页码映射和 hash 稳定性；
- page map 的 exact/chunk/unavailable 降级和禁止伪造单页；
- 状态机非法转换、generation CAS、租约领取、fencing token 和过期恢复；
- LLM 文档内并发、全局并发、公平性和重试分类；
- 短节点不调用模型但进度仍正确；
- 一个节点失败不会被静默写成成功产物，重试只调用未缓存节点；
- FTS/向量 builder 成功但 finish revision 失败时，不推进任何文档 indexed version；
- 本地 FTS 降级与 Milvus 返回相同的定位字段和有界结果；
- staging allocation 并发预留不突破 generation 配额，异常释放和启动 reconciliation 后记账正确；
- 删除 tombstone 未完成 delete-by-doc 或 flush 时，finish 不推进该文档 indexed version；

### 17.2 服务层测试

- 401 页 PDF 通过 mock MinerU 产生 3 个分段任务并最终只创建一份文档；
- 一个分段失败后只重试该分段；
- 服务在 submitted、polling、normalized、enriching 各阶段重启后正确恢复；
- 私有 MinerU 丢失某个远端 chunk task 时只重提交对应分段；
- 慢 LLM 调用期间，另一份同知识库文档能够进入 enrich 并向模型发请求；
- 慢 LLM 调用不持有 `workspace_lock`，删除/发布等短操作可以完成；
- 新 generation 先失败不破坏旧 generation，后成功再原子切换；
- 删除与迟到 worker 并发时不复活文档或留下可检索孤立数据；
- 分别在 generation artifact 完成、snapshot rename、SQLite revision commit 和 legacy 投影更新后模拟
  崩溃，验证在线 runtime 始终读取 SQLite 指向的完整 snapshot；
- FTS/向量构建期间上传新文档时，新请求使用 committed snapshot 本地扫描；只有 finish 成功后新
  runtime 才重新使用 Milvus，且文档 indexed version 不会在 finish 前推进；
- 持有旧 `WorkspaceSnapshotHandle` 的 runtime 与 GC 并发时仍可完成读取；释放最后一个 handle 且超过
  保留期后，snapshot 及其无引用 generation artifact 才可清理；
- 首次上传发布的 SQLite 事务失败时，upload operation、文档版本和知识库 revision 都不发生部分提交；
  重试后只提交一次并返回同一 document。

### 17.3 跨端与迁移测试

- API schema、前端类型和阶段文案一致；
- 前端展示页/段/节点进度、失败阶段和 degraded index；
- 旧 SQLite 数据库向前迁移并可继续轮询既有 MinerU 任务；
- FTS 不可用时使用同一 committed snapshot 本地扫描，应用仍可运行且 UI 不显示“索引成功”；
- FTS/vector 分别 disabled、failed、building 的组合都得到确定的文档状态和 warning；
- pipeline API 同时区分 MinerU `state` 与本地 `dispatch_state`，文档响应暴露当前已发布 revision；
- API/SSE 未变化；若未来将进度改为 SSE，再补齐 CRLF、分块和结束语义测试。

## 18. 实施顺序

为控制风险，按以下顺序落地，每一步保持可运行：

### 第一阶段：建立不可变 workspace revision（已完成）

- 增加 generation、workspace revision 表和 V2 snapshot schema；
- 让 KnowledgeBase、FTS 和向量 builder 支持 V2 snapshot，同时保留 V1 CLI reader；
- 实现单向发布协议、启动 reconciliation 和 legacy workspace 投影；
- 用崩溃点测试固定文件系统与 SQLite 的一致性语义。

这一阶段只改变产物发布和读取方式，不改变 MinerU、摘要和索引产品行为，为后续并发奠定边界。

### 第二阶段：拆出 LLM 增强（已完成）

- 增加 document pipeline 字段和 enrichment task；
- normalize 暂时仍只处理单个 MinerU 结果；
- 把 `build_workspace_doc` 移出 `workspace_lock`；
- 增加单 event-loop 增强调度器、共享模型限流和调用结果缓存；
- 发布时使用 generation 校验和短锁。

这一阶段先解决模型侧没有并发、LLM 无独立状态和失败无法单独重试的问题。

### 第三阶段：超长 PDF 分段（核心完成）

- 引入 `pypdf`；
- 扩展 parse task 的 chunk 字段和唯一约束；
- 实现物理拆分、分段提交、聚合进度与局部重试；
- 增加 normalize task、资源隔离和页码映射。

分段、合并、资源隔离与结构化引用改写均已完成；`content_list` 能无歧义定位全部 block 时生成精确
页映射，输入不完整、重复或逆序时按设计降级，不猜测页码。

### 第四阶段：索引隔离和完整状态衔接（已完成）

- 在第一阶段已使用 V2 snapshot、移除构建长锁的基础上，完善索引任务的 revision fencing；
- 增加原子 finish、蓝绿 collection 延迟清理和 FTS 本地扫描降级；
- index 完成/失败后 reconciliation 文档状态；
- 前端增加阶段进度与错误展示；
- 增加 snapshot handle/refcount、staging allocation 和旧 generation 的保留期 GC。

每阶段均先运行定向 API/索引测试，再运行 `make lint`、`make typecheck`、`make test`；涉及前端后运行
Vitest、`npm run typecheck` 和 `npm run build`。

## 19. 取舍与未采用方案

### 19.1 只提高 MinerU 页数限制

限制属于上游能力且不同部署可能不同，不能作为应用正确性的基础。即使上限提高，超大扫描件仍有
超时、显存和失败重跑成本问题。

### 19.2 只使用 `page_ranges`

当前配置是 profile 级全局过滤，会让所有后续 PDF 静默遗漏未选页；部分 MinerU 部署仍可能按源文件
总页数拒绝。它应保留为用户主动选择页范围的高级能力，而不是自动分段实现。

### 19.3 把分段建成多份文档

实现简单，但破坏文档语义、目录结构、摘要、删除和引用体验，也会让 Agent 把同一 PDF 当成多个来源。

### 19.4 立即引入外部队列

当前产品是单机、SQLite、单 API worker 的部署形态。先把任务事实、租约和幂等边界建立起来，未来
executor 可替换为独立 worker，而无需改变阶段契约。直接引入 Celery/Redis 不能自动解决工作区长锁、
generation 覆盖和产物一致性问题。

## 20. 完成标准

- 401 页及更长 PDF 能自动分段、解析、合并并作为一份文档检索；
- UI 能区分解析、合并、LLM 增强、索引和具体失败阶段；
- 同一知识库至少两份文档可同时向模型发出请求；
- 任意时刻模型请求不超过配置的全局上限，且指标能显示活跃与等待数量；
- LLM 执行和索引构建不再长时间持有 `workspace_lock`；
- 任一分段失败只重跑失败分段，增强失败不重跑 MinerU，索引失败不重跑 LLM；
- 服务在任一阶段重启后能够从 SQLite 恢复；
- 迟到任务、删除和新 generation 并发不会覆盖当前产物；
- SQLite 当前 revision 始终指向完整且 hash 匹配的 V2 snapshot，在线 runtime 和索引 builder 不读取
  根目录 V1 投影；
- 在途 reader、索引任务和 retained snapshot 引用的文件不会被 GC，释放引用后能按保留期清理；
- fencing token 阻止过期 worker 续租、更新进度或发布结果；
- FTS/向量状态、启用/关闭组合、文档状态和知识库 revision 保持一致；FTS 不可用时使用同 revision
  的有界本地扫描，且不伪装为远端索引成功；
- 增强重试复用输入 hash、模型指纹和 prompt 版本均匹配的成功调用，不重复消耗已完成节点；
- 首次上传 operation 与文档/知识库 revision 原子提交，幂等重试不创建重复文档；
- 源 PDF、原始 MinerU 结果、页码映射和最终节点定位字段可追溯；
- 迁移、服务层、索引和前端测试全部通过，diff 不包含运行时数据或模型生成临时产物。
