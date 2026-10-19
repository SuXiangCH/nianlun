# Agent 应用发布与 API 调用设计

> 状态：首版已实现，已完成设计复审、实现复审和本地验证；尚未部署到生产。
> 日期：2026-10-15
> 已确认：知识库自动使用最新已提交内容，不固定为应用发布时的知识库版本。
> 范围：单实例 Nianlun，供其他业务系统通过服务端 API 调用已有 Agent。
> 评审修订：2026-10-15，补齐模型配置匹配、v1 checkpoint、运行参数隔离及最终答案事件规则。

> 版本管理更新：2026-10-19，版本创建与渠道上线已拆分，支持历史版本发布、配置比较、复制草稿与部署记录。
> 当前交互与接口以 [Agent 版本与渠道发布](agent_version_management_design.md) 和 [操作文档](../application_api.md) 为准。

实现说明：凭据与客户端管理收在 `ApplicationReleaseService`，公开调用由
`ApplicationInvocationService` 编排，复用 runner 与聊天 repository 的事务方法。
发布页面分别显示草稿发布就绪与当前发布依赖就绪，旧版本不兼容不会禁用有效草稿的重新发布。
调用记录支持客户端、状态过滤和分页；时间范围筛选留待后续。

## 1. 设计结论

采用“应用草稿 + 不可变发布版本 + 应用级 API 访问”的方案。

- 控制台负责创建、编辑、调试和发布应用；外部业务系统只调用公开运行接口。
- 新会话绑定当前发布版本；已有会话继续使用创建时的发布版本，避免对话中途更换模型或 Agent 配置。
- 每轮调用读取最新 committed 知识库 snapshot，并在该轮执行期间保持同一内容版本。
- API Key 绑定应用及调用客户端，不授予控制台、模型管理、文档管理权限。
- 复用现有 Agent 执行和聊天持久化能力，增加发布、访问控制、调用编排；Agent 核心不依赖 HTTP 或 SQLite。
- 第一版保持单后端进程、单 worker，通过网关隔离公开接口和私有控制台。已完成会话使用持久化 checkpoint，支持服务重启后继续对话。

“发布”表示创建一份可调用的应用配置，并将新会话入口切换至该版本，不表示启动一套新容器。
配置发布也不承诺冻结第三方模型行为、旧版 Python 可执行代码或历史知识库内容。

### 1.1 方案比较

| 方案 | 优点 | 代价 | 决策 |
| --- | --- | --- | --- |
| 现有 chat 接口加启用开关和 Key | 改动少 | 编辑会影响线上，调试/外部会话容易混用 | 不作为正式发布方案 |
| 配置发布版本 + 独立运行入口 | 复用现有架构，有明确线上边界 | 需要版本、调用方和会话持久化 | 推荐 |
| 每次发布独立服务/容器 | 代码和资源隔离强 | 引入部署调度、资源管理和运维体系 | 后续确有隔离需求再做 |

## 2. 现有基础与需要补齐的边界

| 现有实现 | 可复用内容 | 必须调整 |
| --- | --- | --- |
| `apis/v1/apps/api.py` | 应用创建、查询、删除 | 增加编辑、发布、停用、客户端管理 |
| `apis/v1/chat/api.py` | blocking / SSE 接口 | 保留为私有调试接口，公开入口单独定义契约 |
| `services/application_service.py` | 模型/知识库解析、runtime 缓存 | 支持发布配置解析，区分草稿与发布版本 |
| `services/chat_service.py` | 消息、来源、流式取消 | 提取共同执行路径，支持可信调用上下文和 run 持久化 |
| `database/models/chat.py` | 会话、消息、证据 | 增加会话来源、所有者、发布版本和上下文状态 |
| `lead_agent/factory.py` | Agent 组装 | 注入 checkpointer；CLI 原有默认行为保留 |
| `services/workspace_snapshot.py` | committed snapshot 与 reader 引用 | 在请求内固定 snapshot，避免解析/构建/GC 间竞争 |

现有 `ChatRequest.user_id` 没有传入聊天 service，不能直接视为已经实现用户隔离。
现有 runtime 使用 `MemorySaver`，数据库消息历史也不能替代 Agent checkpoint。
现有工作区 snapshot 引用和会话锁在进程内，不能因新增 API 就启用多 worker。

## 3. 产品范围与用户流程

### 3.1 第一版交付

1. 应用编辑与控制台调试。
2. 发布前校验、版本列表、重新发布、停用和恢复。
3. 每个应用创建多个调用客户端，每个客户端管理自己的 Key。
4. 普通 JSON 和 SSE 调用；返回回答、引用证据、用量、运行标识和版本信息。
5. 外部多轮会话隔离、已完成上下文持久化、调用重试去重。
6. 基础限流、并发限制、超时、取消和脱敏调用记录。
7. 接口文档、Python/cURL 调用示例、必要迁移及测试。

首版不包括 OpenAI 协议兼容、多租户账户/计费、浏览器直接使用应用 Key、异步任务队列、
SSE 断线续传、多副本部署、自动执行旧程序版本、长期固定知识库版本。

### 3.2 控制台流程

```text
创建/编辑 Agent -> 调试 -> 发布前检查 -> 确认配置 -> 发布 r1
                                              |
                                   创建调用客户端并配置 Key
                                              |
                                   查看接口地址与调用示例

修改草稿 -> 调试 -> 发布 r2 -> 新会话使用 r2，已有会话保留 r1
停用 API -> 拒绝新的调用和续聊，已受理的请求按原规则结束
```

发布与客户端创建分别完成，避免一次失败留下“已发放凭据但发布状态不明”的跨操作问题。
没有客户端不妨碍发布，只是没有任何外部调用方能够通过鉴权。

## 4. 发布语义与状态

### 4.1 分开表达草稿、发布版本、入口状态

- `applications`：可编辑配置，沿用 `config_version` 表示草稿配置版本。
- `application_releases`：不可变配置记录，例如 `r1`、`r2`。
- `application_deployments`：一个应用一行，保存 `enabled`、`active_release_id` 和乐观锁 `version`。
- `has_unpublished_changes`：比较草稿有效配置指纹和当前 release 指纹，计算得到，不持久化第二份状态。

UI 展示“未发布 / 已发布 / 已停用”，另行提示“有未发布变更”；不把它们揉成互相冲突的单一状态。
从未发布的应用没有 active release。停用保留 active release，恢复时重新校验依赖。

### 4.2 发布版本冻结哪些内容

保存有明确 schema version 的配置快照，至少包含：

- 应用 ID、发布序号、草稿 config version、配置摘要和发布时间。
- `agent_version`、明确的 LLM Profile ID、模型名称、provider、base URL、上下文窗口等实际生效配置。
- `knowledge_base_id` 和固定值 `knowledge_update_policy=latest_committed`。
- Agent 可配置行为，例如澄清开关；首版由发布配置决定，外部请求不能覆盖。
- `PROMPT_VERSION`、`AGENT_TOOL_SCHEMA_VERSION` 和 checkpoint 兼容版本，用于判断当前程序能否执行该 release。
- temperature、thinking、loop guard 的明确值；公开运行不得从 CLI 环境重新解析这些参数。
  兼容指纹同时覆盖 v0 prompt、v1 retrieval/answer prompt 与 checkpoint schema。

不保存模型 API Key，不把凭据放入配置 JSON、摘要或响应。
发布时解析默认模型为具体 Profile ID，之后修改系统默认模型不影响已发布应用。
应用名称、描述等展示字段不改变执行配置；执行字段修改才推进草稿 config version。

### 4.3 模型 Profile 与凭据

release 保存非敏感配置，运行时只从被引用 Profile 获取当前凭据，因此凭据轮换可以立即生效。
为防止“旧 endpoint 搭配新用途凭据”，首版对仍被已启用发布入口或未过期 API 会话引用的
LLM Profile 禁止修改模型、endpoint、provider 和上下文窗口，返回 409 并提示复制为新 Profile。
允许修改名称、默认标记和凭据；清空凭据导致调用失败时应可观察，不得回退到 CLI 环境变量。
所有模型编辑/删除路径都检查引用；历史 release 只保留配置审计信息，不永久阻止资源清理。

知识库的 Embedding Profile 属于知识库配置，跟随当前索引版本；不伪装成由应用 release 冻结。
恢复入口和每次受理都比较 Profile 的非敏感执行配置与 release；不匹配返回 409，要求重新发布。
停用期间发生 Profile 修改后，不允许把旧 endpoint 与已改变用途的凭据组合执行。

### 4.4 原子发布

`POST publish` 提交 `expected_config_version` 和 `expected_deployment_version`。

1. 读取草稿并校验具体模型、最新 committed snapshot 和 Agent 兼容性。
2. 生成无敏感信息的规范配置及摘要；不调用真实 LLM 作为发布必要条件。
3. 在一个短事务内重新检查相关配置指纹、引用和两个 version，插入 release 并切换入口。
4. 任一检查失败，保持原 release 生效；配置并发变化返回 409。

首次发布将入口设为启用；已启用时重新发布保持启用；已停用时重新发布仍保持停用，恢复是显式动作。
无配置变化时发布为幂等操作，不制造重复版本。
停用、恢复、发布都增加 deployment version，因此旧页面的发布请求不能覆盖刚发生的停用。
每次受理在入口锁内校验 enabled/Key/版本并登记 run；停用/撤销与受理有明确先后顺序。
已经登记的请求可以结束，尚未受理的请求不得使用旧缓存绕过停用。

版本管理更新后支持发布可兼容的历史版本；渠道上线前校验模型、知识库、当前程序兼容性与入口修订。
程序升级后若旧 release/checkpoint 不兼容，明确拒绝执行并提示重新发布/新建会话，不能静默切换语义。

## 5. 知识库更新语义

每轮受理后获取最新 committed revision，固定 `content_version`、snapshot 路径及 manifest hash。
在执行结束前持有 reader handle，调用记录和响应记录实际 `knowledge_base_content_version`。
更新发生在调用中途时，本轮继续原 snapshot；下一轮使用新 snapshot。

获取 revision 和 reader 引用必须与现有发布/GC 锁配合；不能先读路径、稍后才获取引用而留下删除窗口。
同轮只启用与固定 revision 匹配的 FTS/vector；正在更新或版本不匹配的派生索引不能混入新旧证据。
现有增量构建会原地修改 collection，仅在创建 runtime 时检查 ready/revision 不足以保证这一点。
增加每知识库、每索引类型的进程内读写保护：构建器在首次修改前进入 mutating 状态，
检索在一次远端查询期间取得读保护并重新检查 revision/状态，写入不能与该查询交叉。
mutating 或当前 revision 与请求 snapshot 不同时，本次 FTS 使用 snapshot 本地扫描、vector 停用；
不让一次长 Agent 调用长期持有索引锁。成功构建后才发布新 revision，失败保持不可用状态。
该保护由 service 的索引协调器和注入的检索适配器协作实现，不让 Agent 核心读取 SQLite。
保留 FTS 不可用时同 revision 本地扫描降级，记录实际检索能力和降级原因。
向量不可用时保持既有可选能力语义；没有可读取的 committed snapshot 则返回 503。
新文件解析/索引失败不影响仍可读取的旧 committed 内容；检查依据是 snapshot 可用性而非仅看一个 status 字符串。

多轮历史可能包含先前版本的回答，故“最新知识库”保证本轮检索范围，不保证历史回答自动作废。
文档删除后不再参与新一轮检索，已有消息及证据按原持久化规则保留；这不是历史数据擦除功能。

## 6. 调用方与密钥

### 6.1 两层身份

- `api_client`：应用下的一个接入系统，例如“业务后台”；由服务端生成不可由请求冒充的 client ID。
- `api_key`：客户端的一把凭据。轮换时新旧 Key 可短期共存，client ID 保持不变，会话能够续用。

Key 只能调用所属应用；合法 Key 访问其他应用返回 404，缺失、错误、过期或撤销 Key 返回 401。
有权限但应用停用返回 403。对不存在或不属于当前所有者的会话统一返回 404。

`user_id` 是接入系统声明的终端用户标识，只在对应 client 内有意义，不是 Nianlun 的登录认证。
因此该方案面向可信服务端接入；若同一客户端内部还需要不互信终端用户访问，必须由接入系统鉴权，
以后再增加用户令牌机制。不得在前端页面、浏览器 localStorage 或 URL 中分发应用 Key。

### 6.2 遵守仓库的密钥只写契约

仓库要求 API Key 不得出现在响应中，所以不采用“服务端生成并通过响应返回一次明文”的常见做法。

控制台使用浏览器安全随机源生成 32 字节 secret 和随机 key ID，临时展示给操作者，
将完整 Key 作为敏感写入字段提交。浏览器仅在注册成功后显示为可用，关闭对话框即清除内存。
自动化管理员在本地生成后按相同写入契约注册。
格式为 `nl_app_<key_id>.<secret>`，服务端校验长度/字符格式，按 key ID 查找并恒定时间比较摘要。
仅保存 SHA-256 摘要、key ID、客户端引用、名称、创建/到期/撤销时间；高熵 token 不使用密码式慢哈希。
列表不返回 secret，也不回显写入字段；创建失败时客户端不得宣称该 Key 已生效。

敏感字段设置 `repr=False` 并脱敏校验错误：现有 validation handler 会带回 Pydantic input，
新增敏感写入接口前必须修正这条泄漏路径，不能只依赖字段 repr。
Authorization、Key 请求体、模型凭据不进入应用/代理访问日志、追踪或测试快照。

撤销和到期影响之后的受理及结果查询；不承诺远端模型已经受理的请求立即停止计费。

## 7. 对外 API 契约

### 7.1 路径与请求

公开入口：`POST /api/v1/runtime/apps/{application_id}/chat`。

```http
Authorization: Bearer <application-key>
Idempotency-Key: <caller-generated-uuid>
Content-Type: application/json
```

```json
{
  "message": "请概括报销要求",
  "user_id": "employee-123",
  "conversation_id": null,
  "response_mode": "blocking"
}
```

`message` 沿用现有 1..32000 字符限制；`user_id` 必填，长度 1..128，拒绝空白值。
`conversation_id` 只能使用服务端已生成的 ID；省略或 null 表示创建新会话，不能传未知 ID 隐式建会话。
`response_mode` 取 `blocking` / `streaming`，默认 blocking；额外字段拒绝。
首版要求 `Idempotency-Key`，长度不超过 128；调用方每个逻辑消息生成一个，重试复用。
不允许请求指定 release、模型、知识库、工具、provider 或密钥，防止覆盖已发布配置。

### 7.2 普通响应

沿用 `{code, message, data}` envelope、既有 HTTP 状态码和 `X-Request-Id`。
新增独立 `PublicChatRequest` / `PublicChatResponse`，不修改私有 ChatResponse 的含义。
`data` 包含：

| 字段 | 含义 |
| --- | --- |
| `app_id`, `release_id` | 实际应用与会话发布版本 |
| `run_id`, `conversation_id`, `message_id` | 执行、会话和回答定位 |
| `answer` | 最终回答，或澄清问题文本 |
| `retrieved_snippets` | 引用证据，保留来源 ID、文档/节点/行号、偏移、版本和截断信息 |
| `usage`, `ttft_ms` | 用量与首 token 延迟，未知时为 null |
| `clarification` | 沿用现有结构；接入方在同一会话回复澄清 |
| `knowledge_base_content_version` | 本轮实际知识库版本 |

不公开原始消息历史、内部 trace、工具参数、文件系统路径和模型 endpoint。
公开 snippets 通过显式字段白名单映射，不能将 Agent 任意 dict 原样透传。

### 7.3 SSE

受理、鉴权、归属、发布状态、限流等校验必须在发送 SSE 头之前完成，失败返回正常 JSON HTTP 错误。

事件序列：`ready -> message* -> done`，执行失败为 `ready -> message* -> error`。

- `ready`：run、会话、release、知识库版本标识。
- `message`：首版从成功执行结果输出一次完整的最终答案 delta，再发送 done；
  不依赖可选的 answer 阶段事件，不转发 candidate，避免漏发或重复拼接。
- `done`：完整 PublicChatResponse 的 data；以持久化成功为前提，是最终答案的权威来源。
- `error`：沿用 `code`、`message`、`request_id`；恰好一个终止事件，之后关闭连接。
- 等待时每 15 秒发送 SSE 注释心跳；心跳不属于业务事件。

复用 `common/sse.py` 编码和现有异步桥接、取消能力；不改变私有聊天事件。
不同 Agent 版本可能只能在生成结束后输出 answer，首版如实保留这一能力差异，不伪造逐 token 流式输出。
客户端必须等 done 才认为回答完整；连接关闭且没有终止事件视为结果未知，按原幂等键查询/重试。
不支持 Last-Event-ID 续传；已完成请求的流式重放仅发送 ready、done，不重放历史 token。

### 7.4 结果查询与重试

`GET /api/v1/runtime/apps/{application_id}/runs/{run_id}?user_id=...` 查询调用状态与最终结果。
需有效同 client Key 且 user_id 匹配。公开查询不返回内部异常或未完成候选内容。
POST 受理后通过 `Location` 返回该资源地址，包括受理后产生的 JSON 错误；浏览器使用场景需正确暴露相关 header。

幂等唯一范围为 `(application_id, api_client_id, user_id, idempotency_key)`，
请求指纹包括 message、conversation_id、response_mode 和公开协议版本。

| 同键再次请求 | 行为 |
| --- | --- |
| 指纹不同 | 409，不执行 |
| 正在执行/等待取消完成 | 409 + Retry-After + Location，不新建会话或调用模型 |
| 已完成 | 返回已存结果；streaming 只发送 ready、done |
| 已失败/取消/中断 | 返回原终态错误及 Location，不自动重跑 |

重放仍检查当前 Key、应用 enabled 和会话所有权，不能用幂等缓存绕过停用/撤销。
校验失败或限流发生在受理前，不占用幂等键；请求结果与幂等记录默认保留 30 天，超过窗口不承诺去重。
这保证保留期内避免本地重复执行，不承诺远端模型恰好执行一次或费用回退。

## 8. 会话、上下文与故障恢复

### 8.1 会话归属

外部会话必须满足 `channel=api`，并匹配 application、client、user、release。
控制台会话为 `channel=console`，公开 API 不能读写；控制台聊天列表默认也不混入外部会话。
已有控制台会话迁移为 console，不推断为 anonymous API 会话。
内部 thread ID 使用服务端生成的 conversation ID 和固定 API namespace，不拼接用户可控 user_id。

新会话绑定 active release；续聊使用自身 release，并检查当前应用仍启用。
同一会话只允许一个活跃 run，后续并发请求立即 409；不在会话锁上无限排队。
首版会话空闲有效期默认 30 天，过期后续聊 409 并要求创建新会话，不静默清空上下文。
不同轮可读取不同知识库 revision；这不改变会话绑定 release。

### 8.2 持久化 checkpoint

factory 接收可注入 checkpointer；公开运行使用已有生态的 SQLite checkpoint 实现，CLI 保留当前默认值。
v0 将 checkpointer 注入主图；v1 注入保存 conversation_context 的外层图，内层检索图不保存跨轮 checkpoint，
避免每轮临时 run_id 产生独立持久化 thread。
checkpoint 使用独立本地数据库，减少对业务 SQLite 长事务的干扰；依赖选型及版本在实现时验证并更新 lock。
所有同 namespace 的 runtime 使用共享持久化 checkpointer，因此知识库更新导致 runtime 重建时不丢失已完成上下文。
进程启动只初始化一次，由 service container 管理关闭和清理生命周期。
checkpoint thread 与会话一对一，过期/删除会话时同时清理；清理失败持久登记待重试，不能静默遗留。
删除应用时其会话由数据库级联删除，因此清理线程前必须先按应用收集会话 ID，再逐条删除对应 checkpoint thread。
OpenAI 兼容入口由调用方自带完整 messages、不建会话，属于无状态调用：该路径的 checkpoint thread 在单次调用结束后立即回收，
不进入续聊，也不随会话过期清理。

配置快照、聊天 SQLite 和 checkpoint 数据库不组成分布式事务，因此采用明确的保守恢复策略：

- 一轮业务状态、消息和最终结果在同一业务数据库事务中提交；提交后才能发 done。
- 只有终态 completed 的会话保证可在重启后续聊。
- 进程崩溃、执行失败、超时或取消导致 checkpoint 完整性无法确认时，将会话标记 `context_state=interrupted`。
- interrupted 会话保留审计和结果查询，但拒绝续聊并要求新建会话；首版不尝试自动回滚 checkpoint 或恢复半轮 Agent 执行。
- 启动时将本进程遗留非终态 run 收敛为 interrupted，并同步消息/会话状态；不自动重新调用模型。

### 8.3 取消与超时

blocking 和 streaming 共用可取消执行控制器，保持 async route 与阻塞模型调用隔离。
客户端断开或总超时后设置取消标记，不再接收/发布后续业务结果。
取消请求与 worker 退出分开：worker 未退出前保持会话占用和实际并发计数，避免“已超时但后台仍执行”不断堆积。
状态由 `running -> cancelling -> cancelled/timed_out` 收敛；原 worker 结果必须通过状态/执行 token 校验，不能晚到覆盖终态。
已完成事务先于断开发生时结果保持 completed，可查询，不能再覆盖成 cancelled。
上游 SDK 配置有界超时和重试；线程不能强制终止，异常挂起时拒绝更多工作并暴露运行状态，不伪装释放资源。

## 9. 数据模型与一致性

| 表/变更 | 核心字段与约束 |
| --- | --- |
| applications | 保留现有字段；执行配置更新以 config_version 做 CAS |
| application_releases | id、application_id、sequence、config_schema_version、config_json、config_hash、created_at；唯一 application+sequence |
| application_deployments | application_id 主键、active_release_id、enabled、version、updated_at；release 必须属于同一应用 |
| application_api_clients | id、application_id、name、enabled、created_at |
| application_api_keys | key_id 唯一、client_id、secret_digest、expires_at、revoked_at、created_at |
| application_runs | id、app/client/user/conversation/release/message 引用、幂等键/指纹、状态、错误类别、实际知识库版本、用量、耗时、时间戳 |
| conversations | 增加 channel、api_client_id、external_user_id、release_id、context_state、expires_at；console 所有者字段为 null |
| runtime checkpoint DB | 交给选定 checkpointer 管理；业务层保留兼容版本和清理记录 |

API 会话所有者字段必须完整，不能只靠 service 的松散 dict 约定；使用 schema、类型和数据库约束共同校验。
run 与消息通过同一业务 SQLite 事务开始/完成，避免“运行成功但消息仍 pending”。
首次受理的幂等插入、会话创建/校验和 begin_turn 同事务完成；使用唯一约束防止并发重复受理。
release/deployment 指针和会话归属按 application 一致，repository 写入时验证，必要时增加组合唯一键/外键。
事务只执行短元数据操作，不持有 SQLite 写锁等待模型或流式消费。

新增迁移必须幂等、可升级现有 v8 数据库，旧应用默认未发布，旧会话默认 console。
不自动发布现有应用，不自动生成/分发密钥；原控制台行为继续可用。
checkpoint 新库缺失时不能凭消息表假装恢复；已有 API 会话标记 interrupted，运维可定位故障。

应用有启用发布入口、未过期 API 会话或活跃调用时拒绝硬删除；先停用，再显式确认删除并清理 Key、run、会话和 checkpoint。
仍被 release 活跃引用的旧知识库也需保护，不能只检查当前草稿的 knowledge_base_id。
停用/Key 撤销不删除历史结果。历史清理遵守既有数据安全规则，本设计不授权清空任何现有数据。

## 10. 服务边界与调用链

```text
公开网关 -> Public Runtime Route
              -> ApiAccessService：Key -> 可信 ApiPrincipal
              -> ApplicationInvocationService：入口/所有权/去重/限流/版本/知识库
              -> 共用 Chat 执行协调：run + 消息 + checkpoint + 取消
              -> AgentRuntimeFactory / runner / KnowledgeBasePort
              -> 公共响应白名单转换 -> JSON / SSE

私有控制台 -> ApplicationReleaseService / ApiClientService / 调试 ChatService
                              -> 专门 repository -> SQLite
```

- `ApplicationReleaseService`：草稿更新、发布校验、配置快照、入口状态与 CAS。
- `ApiAccessService` / `ApiClientService`：凭据验证、客户端/Key 生命周期，向下传递有类型的 principal。
- `ApplicationInvocationService`：外部调用编排，不能通过直接调用旧 chat route 绕过受理检查。
- 共用聊天执行协调：复用持久化、blocking/streaming 桥接、证据和失败收敛，不复制两份 Agent 执行实现。
- `ApplicationService`：保留控制台职责，抽出可接收明确配置的 runtime 构建入口，避免创建影子 applications 记录。
- repository：发布、凭据、run 各自持久化边界；需要跨 run/message 的原子操作由同一事务入口实现。

runtime 缓存以执行配置、release、知识库 revision/检索能力及凭据 generation 作为指纹。
runtime cache key 与 checkpoint thread key 分离；前者变化不能隐式删除上下文。
缓存需要有界容量、闲置回收和 in-flight 引用；旧 runtime 的 snapshot handle 等最后一个 reader 结束再释放。
collector、去重、澄清和 status sink 仍为每请求独立对象，不缓存请求状态。

## 11. 管理 API 与界面

管理接口继续位于私有 `/api/v1/apps` 下：

| 方法/路径后缀 | 行为 |
| --- | --- |
| PATCH `/{id}` | 更新草稿，携带 expected_config_version |
| GET `/{id}/publication` | 发布状态、当前版本、未发布变更和依赖可用性 |
| POST `/{id}/publish` | 原子创建版本并更新入口 |
| PATCH `/{id}/publication` | 设置 enabled，携带 expected_deployment_version |
| GET `/{id}/releases` | 分页版本列表，快照响应严格过滤敏感信息 |
| POST/GET `/{id}/api-clients` | 创建/列出调用客户端 |
| PATCH `/{id}/api-clients/{client_id}` | 启用/停用客户端 |
| POST/GET `/{id}/api-clients/{client_id}/keys` | 写入/列出 Key 元数据 |
| DELETE `/{id}/api-clients/{client_id}/keys/{key_id}` | 幂等撤销，不物理删除审计记录 |
| GET `/{id}/runs` | 按客户端、时间、状态分页查询脱敏调用记录 |

应用详情沿用工作台式布局：应用配置、发布与 API、调用记录三个视图。
发布页显示当前 release、发布时间、知识库自动更新策略和草稿差异；发布前展示实际模型与知识库。
Key 列表展示名称、客户端、Key ID、到期/撤销状态；轮换通过添加新 Key、验证接入、撤销旧 Key 完成。
调用记录显示状态、release、知识库版本、耗时、用量和 request/run ID，默认不展开问题和回答正文。
停用提示明确影响后续续聊；删除、撤销等不可逆操作有明确对象信息。
补齐未发布、无 Key、依赖失效、空记录、提交中、失败、禁用及窄屏状态。

## 12. 部署、资源保护和可观察性

### 12.1 单进程，两个访问边界

```text
业务服务 -> HTTPS 公开网关（仅允许 /api/v1/runtime/...）
                         |
管理员 -> 私有控制台入口 -> 同一个 Nianlun 后端进程 -> SQLite / workspace / Milvus
```

后端只监听 loopback 或受网络策略限制的私有地址；公开网关使用明确路径白名单，默认拒绝其他路径。
`/api/v1/apps`、模型、知识库、上传、历史 chat、`/docs`、OpenAPI 不在公开白名单中。
私有控制台通过受控网络或网关认证访问。应用 Key 不具备管理权限，CORS 不能替代访问控制。
网关验证路径规范化/编码/重复斜杠绕过，阻止用户通过公开域名访问旧 chat 和管理接口。
网关关闭 SSE 缓冲并配置合适的 idle timeout，限制请求体大小和未鉴权流量；Key 不出现在 URL。

首版不拆独立 runtime 进程：当前 reader 引用、GC 和锁不是跨进程协议，
且现有 `build_services` 会恢复后台任务、处理 pending 消息，重复启动会产生额外一致性问题。
多进程必须先设计跨进程 reader lease、run owner/lease、共享限流与独立后台任务职责。

### 12.2 初始保护参数

建议起始值：全局 API 执行并发 8、每应用 4、每客户端 2、每客户端每分钟 60 次、单次总时限 300 秒。
这些是保守初值，需结合现有文档增强与模型端容量调优，不是吞吐承诺。
发布设置不允许任意绕过全局资源上限；计数包含正在取消但尚未退出的 worker。
超额立即 429 并给 Retry-After，不建立无限等待队列；内部故障导致容量不可用返回 503。
全局线程池、队列和缓存必须有界，调用限流不能只限制 HTTP 连接数而忽略实际模型执行。
公开运行默认关闭；新增环境变量、默认值、启动校验和 `.env.example` 在实现时同步落地。

记录 request/run/app/client/release/知识库版本、状态、用量、耗时、降级和错误类别。
日志不保存用户全文、完整模型请求、工具参数或密钥；业务消息按已有数据库规则保存，明确与日志的区别。
校验失败、401/404 等未受理请求只做有界日志/指标，不为任意外部输入无限写业务 run。

## 13. 错误与验收

| 情况 | 结果 |
| --- | --- |
| 请求格式、字段或必填幂等键不合法 | 422 |
| Key 缺失/无效/过期/撤销 | 401 |
| 有权限但应用或客户端停用 | 403 |
| 应用、会话、run 不存在或不属于调用方 | 404 |
| 版本冲突、会话忙、上下文中断、同键不同请求 | 409 |
| 并发或频率超过上限 | 429 + Retry-After |
| 模型上游失败 | 502，脱敏消息 |
| snapshot/配置/checkpointer 等运行依赖不可用 | 503 |
| 总时限到期 | 504；worker 退出前保持占用 |

已有 envelope 不变，不靠解析错误中文文案实现客户端重试。
结果资源通过有类型的 `status` 和 `error_type` 表达已受理调用的机器可读状态。
受理前失败不产生 run；受理后 SSE 错误使用同等 code 语义，HTTP 已发出后不能改状态码。

验收必须覆盖：

1. 编辑不影响已发布配置；默认模型变化不影响 release；并发发布/停用不丢更新，发布失败旧版本可用。
2. 新旧会话分别使用新旧 release；知识库下一轮更新、单轮 snapshot 一致、降级可观察。
3. 跨 app/client/user/console 的会话与 run 访问拒绝；Key 轮换可续聊，撤销立即阻止新受理。
4. Key 写入成功/失败、校验错误、异常、日志、OpenAPI 示例都不泄漏秘密；所有敏感响应采用白名单。
5. 同键并发只执行一次；新会话重试不额外建会话；完成重放、终态失败、保留期和 busy 行为明确。
6. SSE 预检、事件顺序、candidate 过滤、终止事件、心跳、分块边界和取消均正确，私有协议无回归。
7. 已完成会话重启可续聊；知识库导致 runtime 重建不丢上下文；中断会话明确失败且不会重复调用模型。
8. 超时/断开后 worker 未结束时不释放配额，晚到结果不覆盖终态，落库后断线可查询结果。
9. v8 数据库向前升级且重复迁移安全；旧应用未发布、旧会话保持 console；删除与过期清理无孤立 checkpoint。
10. 公开网关不能访问旧 chat、管理接口、文档及 OpenAPI，编码路径不能绕过；后端端口无法从公开网络直连。

自动测试使用 fake Agent、临时 SQLite、临时 checkpoint 与小型确定性 snapshot，不调用真实外部服务。
实现阶段运行对应 API/Agent/索引测试；共享边界改动运行 `make lint`、`make typecheck`、`make test`，
跨端协议与页面改动运行前端 Vitest、typecheck、build；部署增加真实网关配置的集成检查。

## 14. 实施顺序与规模

| 阶段 | 可验收结果 | 估算 |
| --- | --- | --- |
| 1. 发布模型 | 草稿编辑、release/deployment、CAS、模型引用保护、迁移 | 2-3 人日 |
| 2. 调用身份 | client/Key、敏感字段脱敏、所有权与网关边界 | 1-2 人日 |
| 3. 可靠执行 | 统一受理、持久化 checkpoint、run/幂等、取消、限流、JSON/SSE | 3-5 人日 |
| 4. 控制台与交付 | 发布/Key/记录页面、示例、部署配置、端到端验收 | 2-3 人日 |

完整首版预计 8-13 人日，假设一名熟悉仓库的开发者，现有 Agent 行为和上游服务稳定。
较“开关 + Key”的基础版增加的工作主要来自发布隔离、上下文持久化、取消及重试一致性。
代码复用程度和 checkpoint 兼容性需在实现前的小型验证中确认，再收敛排期。

此文档只定义设计；没有修改运行代码、启动外部服务、创建提交或授权数据清理。
