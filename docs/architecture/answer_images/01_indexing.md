# 图片目录与文档版本

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md)

## 职责与依赖

生成可信图片目录，绑定已提交的文档版本，为已有文档提供离线补建。

输入为归一化产物和最终节点正文；输出为不可变图片 bundle 与 snapshot v3。下游由 [Agent 模块](02_agent.md) 按窗口读取。

## 计划改动位置

以下是设计约定的改动位置。多个模块共用契约和实现，不建立重复流程。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 修改 | `app/api_server/services/documents/normalization.py` | 沿用本地资源归一化和登记，拒绝越界路径和外部下载。 |
| 修改 | `app/api_server/services/documents/pipeline_contracts.py` | 补充图片目录产物的严格类型。 |
| 修改 | `app/api_server/services/document_pipeline_service.py` | 最终正文和树确定后生成目录，纳入发布和失败补偿。 |
| 修改 | `app/api_server/services/workspace_snapshot.py` | manifest v3、目录指针、资源校验、v2 兼容与资源清理。 |
| 修改 | `app/api_server/services/workspace_store.py` | 纳入图片 bundle 的工作区发布和生命周期。 |
| 修改 | `nianlun/knowledgebase/workspace_view.py` | 从当前所持 snapshot 读取正确版本目录。 |
| 新增 | `app/api_server/services/documents/image_catalog.py` | 解析图片、生成节点字符 occurrence 并封存 bundle。 |
| 新增 | `app/api_server/backfill_document_images.py` | 最小装配的离线补建、dry-run、CAS 发布及逐文档报告。 |
| 新增 | `app/api_server/services/storage_maintenance_lock.py` | 同一存储的共享/独占生命周期维护锁。 |
| 修改 | `app/api_server/services/container.py` | 首次存储访问前取得共享维护锁，覆盖整个运行生命周期。 |
| 修改 | `app/api_server/main.py` | 接入维护锁释放与启动失败处理，不能等迁移后才检查锁。 |

<a id="s5"></a>

## 图片目录与版本绑定

<a id="s5-1"></a>

### 建立目录

图片目录在文档最终正文和树索引确定后生成，作为同一 generation 内的不可变派生产物发布。
目录和用于展示的图片副本统一放在独立命名空间：

```text
artifacts/<document_id>/g<generation>/image_catalogs/v1/<bundle_digest>/
  images.json
  assets/<sha256>.<extension>
```

`bundle_digest` 由目录格式版本、规范化后的目录记录与资源摘要确定，不能依赖临时路径。
图片目录记录的资源位置均在这个已封存目录内。这样新入库和旧文档补建使用同一格式，
无需向已经封存的 `enriched/` 或 `normalized/` 目录写文件。

目录至少记录：

| 字段 | 含义 |
| --- | --- |
| `image_id` | 当前文档 generation 内稳定的图片标识 |
| `document_id`、`generation` | 资源归属 |
| `asset_relpath`、`sha256` | 服务端内部使用的不可变资源路径和内容校验 |
| `media_type`、`size_bytes`、`width`、`height` | 校验和展示所需信息 |
| `orientation` | 校验后的 EXIF 方向枚举，无方向时为 1；原始尺寸不变，展示宽高比按方向调整 |
| `alt_text`、`caption` | 原文已有文字，不含模型生成描述 |
| `occurrences` | 所属节点、节点正文字符区间、原文行号 |

图片的逻辑身份为 `(knowledge_base_id, document_id, generation, image_id)`。
同图多次出现时复用资源记录，保留多处 occurrence；跨文档不合并来源身份。

使用现有 MarkdownIt 解析 Markdown 图片及引用式图片，使用 HTMLParser 处理 `img`。
代码块中的图片语法不作为资源。资源路径只能从归一化后的已登记产物解析，
不允许根据模型输出或任意 Markdown 路径读取文件。

<a id="s5-2"></a>

### 与检索窗口精确关联

occurrence 的字符坐标必须基于与 `get_line_content` 相同的规范化节点文本，
不能把全文字符偏移直接当作节点偏移。为正文规范化、标题增强、CRLF 和中文字符补测试。

只有图片引用的完整语法范围落在本次读取的字符窗口内，才附加其图片引用。
引用被截断时，本次不附图；后续读取完整引用后再加入。仅引用定义位于窗口外不影响
已完整读到的引用式图片使用，但资源解析必须来自该版本的可信目录。
不会因为读取一个短片段而附带同一章节中尚未读取的全部图片。
字符窗口始终基于源节点正文，不因请求级图片编号或模型可见文本的长度变化而改变。
原文与模型投影的分离、跨窗口图片语法的遮蔽见 [读取与投影契约](02_agent.md#line-content-projection)。

<a id="s5-3"></a>

### Snapshot 契约

拟升级 snapshot manifest schema 到 v3，新增可选 `image_catalog_relpath`。
目录和它引用的图片文件都必须出现在 `artifact_files` 中并具有大小与摘要记录。
发布、复制前一版本记录、文档删除、孤儿清理和 generation 清理都必须识别这组资源。

读取器兼容 v2/v3；v2 没有图片目录时按纯文本处理，不修改已发布 manifest。
新版本登记图片失败时，不发布一份声称图片完整可用的目录。可选图片存在格式不支持等
问题时，记录具体不可用状态；只有成功校验的图片进入可展示资源集合。

原生上传 Markdown 仅引用外部 URL、或缺少本地文件时不自动下载；首版无法展示这些图片。

<a id="manifest-capacity"></a>

### 资源闭包与发布预算

沿用全量枚举方案，不改为只登记目录的 sidecar。以 entry 的基础路径、图片目录及其已登记 assets
组成该文档的资源闭包；目录解析和资源摘要必须先通过当前 snapshot 或新 bundle 的校验。
发布、删除、`build_manifest` 的 missing 检查及 artifact 行登记共用此闭包，不能仅保留 entry 的直接路径。
发布 B 或删除 B 均须保留 A 的完整闭包；GC 仍保护有效 snapshot 引用的 generation，不能独立删其目录。

首版保持 10,000 文档、50,000 个唯一文件、32 MiB 序列化 manifest 的现有上限，三者同时成立才可发布。
文件预算为 `F = |所有文档基础路径 ∪ 图片目录路径 ∪ 图片资源路径|`，按实际相对路径去重；
不能因不同路径的字节摘要相同就少登记记录。10,000 是文档计数上限，不承诺任意图片分布都能达到它。
例如 10,000 篇文档各有四个基础文件、一个目录和一张图，合计 60,000 个文件，应明确拒绝该发布。

发布前检查候选闭包的文件数和实际序列化字节数；超限返回预算项、当前值和候选值，建议减少资源或拆分知识库。
不推进 SQLite 指针、不损坏旧 snapshot；已有版本仍可读取，删除文档仍可释放预算。
不按估算的“每条记录平均字节”承诺容量，也不静默遗漏图片以凑进 manifest。

<a id="snapshot-upgrade"></a>

### v3 升级与回滚边界

首版采用停止旧读取器后再写 v3 的升级方式，不支持新旧代码同时使用同一存储滚动发布。
首次 v3 发布前停止全部旧 API、worker 和清理入口并释放读引用，备份一致的 SQLite 与工作区，
部署支持 v2/v3 的同版本代码后再开放写入。维护锁不能证明不参与该协议的旧进程已经退出，
部署流程必须确认这一条件；不得用放宽 schema 或忽略未知字段来替代兼容处理。
新代码读 v2/v3；旧 v2 代码拒绝 v3 属于预期。写入 v3 后只允许回退到支持 v3 的兼容代码；
若必须恢复纯 v2 程序，须另行授权离线恢复整套一致备份，不能仅回退 manifest 或数据库指针。
这些步骤进入 `docs/application_api.md` 的部署说明，本次设计修订不执行备份恢复或停服。

<a id="s5-4"></a>

### 已有文档的本地补建

第一阶段提供拟议的离线管理命令，执行前须停止使用同一存储目录的 API 服务、文档 worker
和清理进程。避免让用户删除重传已有文档，也不复用只允许失败文档调用的 `/retry` 接口：

```bash
uv run python -m app.api_server.backfill_document_images \
  --knowledge-base-id KB_ID --document-id DOCUMENT_ID --dry-run

uv run python -m app.api_server.backfill_document_images \
  --knowledge-base-id KB_ID --document-id DOCUMENT_ID
```

省略 `--document-id` 时处理指定知识库的已提交文档；没有跨知识库的默认全量修改。
新增 `--batch-size`，默认 100、允许 1–100；单文档模式只提交该文档。
批大小按通过预检查、确需新增目录的候选计数；已有目录、不支持及预检查失败的文档不占批次名额。
命令通过专用的最小 services/repositories 装配执行，数据路径来自 settings。
不得调用 `build_services` 或服务启动恢复流程，避免误将在线消息标为失败、启动任务调度或清理资源。
`--dry-run` 只输出可补建、已有目录、缺少产物和不支持资源的统计，不写业务文件、
修改数据库或推进知识库版本；只允许为进程互斥打开或创建下述固定锁文件。
dry-run 同时报告预算检查结果、待补建数和预计 revision 数；不能把预测当成已完成。

离线约束必须由进程锁保证，不能仅依靠操作提示、PID 文件或进程内 snapshot 引用计数：

- 使用 settings 解析后的同一持久化目录中的固定维护锁文件；所有使用该存储的服务、
  worker 和清理入口在任何存储访问及启动恢复前持有共享锁，直到工作结束并释放所有读引用。
- 补建命令，包括 dry-run，在访问存储前以非阻塞方式获取独占锁，并保持至命令结束。
  有服务使用存储或另一补建正在执行时，明确退出且不执行扫描、写入、恢复或清理。
- 补建期间启动服务也必须在存储初始化前因锁冲突退出，不能先执行数据库迁移或恢复再检测。
  采用与现有 workspace 锁一致的 POSIX 文件锁，锁文件不可在运行中删除或替换；进程退出由系统释放锁。
- 维护锁是运行生命周期锁，不能用只覆盖发布阶段的 workspace 锁替代。
  获取顺序统一为维护锁、workspace 锁、数据库事务；所有独立存储入口接入维护锁后才能交付该命令。

现有 snapshot 引用注册表仅在单个进程内有效。首版不支持补建与服务在线并行；
未来若改为在线补建，须由持有同一引用注册表的服务进程执行，或先引入跨进程读租约及相应 GC 协议。

执行步骤：

1. 按批次获取最新已提交 snapshot 的读引用，记录 content version 和每个目标文档的 generation、
   正文/树摘要；始终以已提交内容作为图片位置基准，下一批重新获取已推进的版本。
2. 从同 generation 的归一化产物定位本地图片，校验后生成独立 bundle；不修改原正文、树或 manifest。
   单文档资源缺失独立报告，不把失败文档声称为已补建。
3. 在 workspace 锁内对该批候选做预算及 CAS 校验，校验预期 content version、generation 与摘要。
   文档已删除则剔除；其他冲突使该批不提交并报告重试，不能发布基于不同版本拼出的资源集合。
4. 对该批成功候选只发布一次 v3 snapshot，以 `content_version + 1` 引用原有内容和新增目录，
   再通过同一发布事务推进 SQLite 指针。事务成功后才把该批文档报告为完成。
5. 已有相同格式、正文/树及资源摘要的目录视为无需处理；全为空或幂等的批次不创建 revision。
   已提交批次不回滚，失败批次可重跑，所有 staging 和未引用 bundle 按现有补偿规则清理。

100 个均可补建的新目录在默认批大小下只产生一个 revision；无冲突时提交次数不超过
`ceil(待新增目录数 / batch_size)`。批量减少全量 manifest 的重复写入，不宣称已消除大库的所有写放大；
每批仍受同一 manifest 上限约束，超限批次不拆成静默部分成功。

补建不调用 MinerU、LLM 或 Embedding，不改变原有摘要和历史消息。
缺少本地图片的文档保持文本可用，并返回明确的补建报告，不根据外链自动补下载。
数据库仍处于处理中状态的文档，只基于其已提交 generation 补建，不接管未完成任务，
不修改正在构建的新 generation；服务恢复后仍通过正常发布校验处理原任务。

content version 推进时必须检查 FTS/vector 的版本约束。第一版保守地将受影响索引标为 pending，
沿用现有可用性降级，不伪造索引 ready，也不由补建命令自动执行外部索引构建。
管理员可在之后显式重建索引；未来只有证明正文、文档集合与索引记录完全一致时才能优化版本复用。
补建产生的 staging、bundle 和 snapshot 同样进入现有发布失败补偿与孤儿清理规则。

## 模块验收

扩展 tests/api_server/test_document_normalization.py、tests/api_server/test_workspace_snapshot.py、tests/indexing/tree/test_workspace.py；新增补建、维护锁与独立进程测试。所有 worker、清理及独立存储命令须逐一审计并接入维护锁，未覆盖前不交付补建入口。

具体边界场景见 [完整验收清单](08_validation.md)。
