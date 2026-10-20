# 年轮企业微信智能机器人渠道设计

- 日期：2026-10-20
- 状态：设计待评审
- 分支：`feat/wecom-publish-channel`
- 关联：`docs/architecture/application_api_publishing_design.md`、`docs/architecture/agent_version_management_design.md`

## 1. 背景与目标

年轮已经具备不可变发布版本、应用部署、公开 API 调用和流式 Agent 运行时。本设计新增企业微信智能机器人长连接渠道，复用同一套发布版本和 Agent 执行能力。

目标：

1. 提供独立的 Python 长连接网关，连接企业微信智能机器人。
2. 平台侧完成渠道配置、发布、启停、状态和会话管理，管理台可操作全链路。
3. 第一版只处理纯文本，单聊和群聊均支持；群聊内所有成员共享同一段年轮会话。
4. 支持单机本地所有权和 Redis 多实例所有权，不裁剪原 TS 网关的 Redis 能力。
5. 使用企业微信官方 Python SDK，并通过适配层隔离 SDK 变化。
6. 端到端可用：配置机器人、发布版本、启用渠道、收发消息、流式回复、重部署、停用和重连。

非目标：

- 语音转写、图片、文件、视频、图文混排。
- 企业微信模板卡片、欢迎语、主动推送、反馈事件。
- 多机器人/应用、多渠道实例、跨机平台数据库。
- 公网 Webhook 接入。

## 2. 关键决策

| 决策 | 结论 |
| --- | --- |
| 网关形态 | 独立 Python 进程，独立部署和重启，不嵌入 API Server |
| 网关职责 | 只做长连接、消息归一化、并发/去重/超时、流式回复和状态上报 |
| 平台职责 | 渠道配置、发布版本、启停、会话/运行持久化、Agent 执行、状态展示 |
| 渠道建模 | API 部署表保持不动；新增 `application_wecom_channels`，独立维护 WeCom 部署状态 |
| 发布版本 | 复用 `application_releases`，WeCom 与 API 各自维护 active release 和 deployment version |
| 内部协议 | 内部 HTTP + NDJSON，不复用公开 SSE 协议 |
| 内部鉴权 | 专用 `NIANLUN_INTERNAL_TOKEN`，不复用应用 API Key |
| 消息身份 | 网关哈希企微单聊 userid / 群聊 chatid 后传给平台，平台不接触原始 ID |
| 群聊会话 | 以 `chatid` 为身份，群内所有成员共享同一段会话 |
| 所有权 | `local` 与 `redis` 两种后端；单机默认 local，多实例使用 redis |
| 配置版本 | 按 `bot_id` 全局单调分配，删除/重建/换绑后不复用；见 5.1.1 |
| 渠道到期 | 支持 `expires_at`；到期后不再建连，管理台显示“已过期”；见 5.9 |
| Python SDK | 官方 `wecom-aibot-python-sdk`，适配层隔离 |
| 文本上限 | 输入 64 KiB、回答 1 MiB、企微回复截断 20 KiB，沿用原网关限制 |
| 部署约束 | 平台仍为单进程 SQLite；网关可通过 Redis 多实例部署 |

## 3. 总体架构

```text
企业微信智能机器人
        │  WebSocket 长连接
        ▼
┌─────────────────────────────────────────────┐
│ channel_gateway（独立 Python 进程）          │
│                                             │
│  FastAPI 控制面  /health /ready /metrics     │
│  ChannelManager  ── SnapshotLoop             │
│  WecomAdapter    ── Ownership(local|redis)   │
│  WorkflowClient  ── StatusReporter           │
└───────────────┬─────────────────────────────┘
                │ 内部 HTTP + NIANLUN_INTERNAL_TOKEN
                ▼
┌─────────────────────────────────────────────┐
│ nianlun API Server（FastAPI + SQLite）       │
│                                             │
│  管理 API  /api/v1/apps/...                  │
│  内部 API  /internal/v1/wecom/...            │
│  ReleaseService / InvocationService          │
└───────────────┬─────────────────────────────┘
                │
                ▼
          React 管理台

Redis（可选）：仅 channel_gateway 使用，保存 Bot 级所有权租约、
代际、冲突栅栏和跨实例状态；平台不依赖 Redis。
```

边界原则：

- 网关不读平台数据库，平台不直连企业微信。
- 网关只通过内部 HTTP 获取快照、上报状态、发起运行。
- 平台内部接口默认只绑定本机，必须由 token 保护。
- Redis 只解决网关多实例所有权，不承担消息、会话或发布数据。

## 4. 官方 Python SDK 核实

### 4.1 结论

企业微信官方提供 Python SDK，信息如下：

- 官方文档：[智能机器人长连接](https://developer.work.weixin.qq.com/document/path/101463) 的 SDK 列表同时列出 Node.js `aibot-node-sdk` 和 Python `aibot-python-sdk`。
- 仓库：[WecomTeam/wecom-aibot-python-sdk](https://github.com/WecomTeam/wecom-aibot-python-sdk)，描述为“企业微信智能机器人 python SDK”。
- PyPI：`wecom-aibot-python-sdk`，当前最新版本 `1.0.2`，作者 `WeCom <wecom@tencent.com>`。
- 许可证：MIT。
- 依赖：`websockets>=12.0`、`aiohttp>=3.9`、`pyee>=11.0`、`cryptography>=42.0`、`certifi>=2023.0`；要求 Python >= 3.8。
- 导入包名：`aibot`；核心类：`WSClient`、`WSClientOptions`。

注意：PyPI 上还存在社区实现 `wecom-aibot-sdk-python`（`chengyongru/wecom_aibot_sdk`），不是本设计使用的包。两者包名相近，必须锁官方包名。

### 4.2 SDK 能力与已知行为

官方 SDK 提供：

- WebSocket 自动认证、心跳、断线重连和串行回复队列。
- `reply_stream(frame, stream_id, content, finish=False, ...)` 流式回复，等待企微 ACK。
- `message`、`message.text`、`connected`、`authenticated`、`disconnected`、`reconnecting`、`error` 事件。
- 事件回调动态分发，若收到 `disconnected_event` 会触发 `event.disconnected_event`。
- 可注入 Logger，必须传入静默 logger，避免 SDK 默认日志输出消息帧或凭据。

需要在适配层处理的 SDK 行为：

1. `WSClient.connect()` 内部捕获连接异常，不向调用方抛出；失败后 `_started` 仍为 `True`，复用同一实例再次 `connect()` 不会重新建连。因此适配层每次建连创建新的 `WSClient` 实例，并等待 `authenticated` 或 `error`/`disconnected` 结果。
2. 认证失败时 SDK 触发 `error`，消息以 `Authentication failed` 开头，但不会自动关闭连接。适配层必须主动 `disconnect()` 并上报 `AUTH_FAILED`。
3. 回复 ACK 失败以 `RuntimeError("Reply ack error: errcode=...")` 抛出。适配层解析 errcode，无法解析时归类为 `WECOM_ACK_UNKNOWN`，不做重试。
4. SDK 默认重连策略可配置。为与 TS 网关一致，本设计设置 `max_reconnect_attempts=0`，由 ChannelManager 在每次重连前重新检查所有权。
5. SDK 版本较新（1.0.x），适配层必须有契约测试；升级 SDK 必须单独评审。

依赖声明：

```toml
"wecom-aibot-python-sdk>=1.0.2,<2.0.0",
"redis>=5.2,<6.0",
```

同步更新 `uv.lock`。

## 5. 平台设计

### 5.1 新增数据表

`application_wecom_channels`：

```text
id                  VARCHAR PK（UUID）
application_id      VARCHAR NOT NULL, FK applications(id) ON DELETE CASCADE
bot_id              VARCHAR(256) NOT NULL
secret              TEXT NOT NULL
default_response    TEXT NOT NULL DEFAULT '暂时无法回答，请稍后再试。'
enabled             BOOLEAN NOT NULL DEFAULT 0
active_release_id   VARCHAR NULL, FK application_releases(id) ON DELETE SET NULL
deployment_version  INTEGER NOT NULL DEFAULT 0
config_revision     INTEGER NOT NULL DEFAULT 1
status              VARCHAR(16) NOT NULL DEFAULT 'offline'
error_code          VARCHAR(32) NULL
expires_at          VARCHAR(64) NULL
last_reported_at    VARCHAR(64) NULL
created_at          VARCHAR(64) NOT NULL
updated_at          VARCHAR(64) NOT NULL
```

约束与索引：

- `UNIQUE(application_id)`：第一版每个应用一个企微机器人。
- `UNIQUE(bot_id)`：同一机器人不能被两个应用同时连接。
- `CHECK(status IN ('offline', 'online', 'error'))`。
- `CHECK(error_code IS NULL OR error_code IN ('AUTH_FAILED', 'NETWORK_ERROR', 'CONNECTION_CONFLICT'))`。
- `CHECK(status != 'error' OR error_code IS NOT NULL)`。
- `CHECK(status = 'error' OR error_code IS NULL)`：`online` 和 `offline` 不允许携带错误码。
- `CHECK(config_revision >= 1 AND deployment_version >= 0)`。
- 索引 `(status)` 可选，用于管理台筛选；不作为第一版必需。

语义：

- `config_revision` 在创建、bot_id/secret/default_response/expires_at 修改、启停变化时递增；部署新版本不递增（首次部署的自动启用属于启停变化，仍需递增）。它按 `bot_id` 全局单调分配，规则见 5.1.1。
- `deployment_version` 在部署、启用、停用时递增，用于乐观并发。
- `enabled=false` 时网关不建连；首次部署版本后自动启用。
- `status` 由网关上报；平台在停用、配置变更时立即置为 `offline`。
- `expires_at` 是可选的 UTC ISO 8601 时间；`NULL` 表示不过期。写入前统一解析并转换为 UTC 后存储，保证字符串比较和网关解析一致。到期不改变 `enabled`，但网关必须断开且不再建连，平台拒绝 `online` 上报；管理台显示“已过期”。将 `expires_at` 延后或清空会递增 `config_revision`，网关据此重新建连。
- `secret` 只写不读；管理接口只返回 `has_secret`。
- 第一版不提供单独删除 WeCom 渠道的接口；渠道随应用级联删除，避免部署事件版本与 checkpoint 清理出现悬空状态。

### 5.1.1 配置版本分配

`config_revision` 是 Redis ownership 的栅栏版本，必须在同一个 `bot_id` 上保持全局单调。仅用渠道行自增会在“删除渠道后用同一 bot_id 重建”时把版本重置为 1，旧租约或冲突版本可能永久阻塞新渠道。因此增加一张不随渠道删除的计数器表：

```text
application_wecom_bot_revisions
bot_id          VARCHAR(256) PK
last_revision   INTEGER NOT NULL
updated_at      VARCHAR(64) NOT NULL
```

分配规则：

- 每次创建渠道、修改 bot_id/secret/default_response/expires_at、启停渠道时，在同一写事务内执行 `next = max(last_revision, channel.config_revision) + 1`，更新目标机器人的计数器并把 `next` 写入渠道的 `config_revision`；新渠道的当前版本按 0 处理。
- 新 `bot_id` 的历史计数从 0 开始；新建渠道首次分配得到 1，换绑已有渠道时必须高于渠道当前版本。
- 修改 `bot_id` 时按新 `bot_id` 的历史和渠道当前版本共同分配，防止同一渠道复用旧 revision 而接受旧配置更新、状态上报或运行请求；旧 `bot_id` 的计数器保留，旧值不会回退。
- 计数器行不随应用或渠道删除；表规模只与历史上出现过的 `bot_id` 数量相关。
- 不允许直接修改 `config_revision`；所有变更都经过该分配器。

这与原 TS 网关“revision 全局单调、删除重建不重置”的约定一致，也是 Redis `acquire/block/recover` 的 revision 比较能够安全工作的前提。

### 5.2 会话模型调整

`conversations.channel` 增加 `wecom`。重建 check constraint：

```sql
(status IN ('active', 'archived', 'deleted'))
AND (channel IN ('console', 'api', 'wecom'))
AND (
  (channel = 'console' AND api_client_id IS NULL AND external_user_id IS NULL AND release_id IS NULL)
  OR (channel = 'api' AND api_client_id IS NOT NULL AND external_user_id IS NOT NULL
      AND length(trim(external_user_id)) > 0 AND release_id IS NOT NULL)
  OR (channel = 'wecom' AND api_client_id IS NULL AND external_user_id IS NOT NULL
      AND length(trim(external_user_id)) > 0 AND release_id IS NOT NULL)
)
```

新增索引：

```sql
CREATE INDEX idx_conversations_channel_peer
ON conversations(application_id, channel, external_user_id, status);
```

`external_user_id` 对 WeCom 存的是网关计算的 `user_key`，不是原始企微 ID。

### 5.3 部署事件表调整

`application_deployment_events` 唯一约束改为：

```text
UNIQUE(application_id, channel_type, deployment_version)
```

并增加：

```text
CHECK(channel_type IN ('api', 'wecom'))
```

已有事件保持 `channel_type='api'`。WeCom 事件使用 `channel_type='wecom'`，`deployment_version` 与 API 渠道独立计数。

WeCom 的部署、启用、停用都要写事件：

- 部署：`action='deploy'`，`channel_type='wecom'`，`deployment_version` 为本次部署后的版本。首次部署自动启用只写 deploy 事件（与 API 一致），不额外写 enable 事件。
- 启用/停用：`action='enable'|'disable'`，`channel_type='wecom'`，`deployment_version` 为本次启停后的版本，与现有 API `set_enabled` 行为一致。

现有查询必须补 `channel_type` 过滤，否则加入 WeCom 事件后会污染 API 渠道结果：

- `PublishingRepository.last_deployed_at()` 固定过滤 `channel_type='api'`，供现有 `PublicationResponse.last_deployed_at` 使用。
- `PublishingRepository.deployment_events()` 增加 `channel_type` 参数；管理 API 默认 `api`，保持现有前端行为。
- WeCom 渠道的 `last_deployed_at` 单独查询 `channel_type='wecom'` 且 `action='deploy'` 的最新事件。
- 顶层的 API 发布字段（`deployment_version`、`active_release`、`last_deployed_at`）继续保持只反映 API 渠道。

### 5.4 运行模型复用

`application_runs` 不新增字段：

```text
client_id          = "wecom:<channel_id>"
user_id            = user_key
idempotency_key    = "wcm_<sha256(channel_id|msgid)>"
request_fingerprint= sha256({user_key, text, protocol_version: 1})
message_id         = 平台生成 UUID（原始 msgid 不进入平台）
```

现有唯一约束 `(application_id, client_id, user_id, idempotency_key)` 和“同一会话最多一个活跃 run”的部分唯一索引继续生效。

`ApplicationRun` 的 `release_id` 固定为本次运行使用的版本；`knowledge_base_content_version` 取发布版本引用的 committed snapshot 版本。`client_id` 是合成标识，不对应 `application_api_clients` 记录；WeCom 运行走独立受理路径 `admit_channel`，不复用 API Key 校验。`protocol_version` 是内部常量 `1`，与公开 API 的指纹做法一致，不出现在请求体中。

### 5.5 发布响应

`PublicationResponse` 保留全部 API 字段，新增：

```python
wecom_channel: WecomChannelState | None
```

`WecomChannelState`：

```text
id, bot_id, has_secret, default_response,
enabled, active_release, deployment_version,
config_revision, status, error_code, expires_at,
has_unpublished_changes, dependencies_ready, dependency_error,
last_deployed_at, last_reported_at
```

- 不包含 `secret`。
- `expires_at` 原样返回；前端据此显示“已过期”，不依赖平台后台定时任务改写状态。
- `active_release` 复用现有 `ApplicationReleaseResponse`。
- `has_unpublished_changes`、`dependencies_ready`、`dependency_error` 按 WeCom 渠道自己的 active release 计算，不能复用顶层 API 渠道字段。
- `last_deployed_at` 取最新一条 `channel_type='wecom'` 部署事件的时间。
- API 渠道字段保持原样，避免破坏现有前端和测试。

`ApplicationDeploymentEventResponse.channel_type` 从 `Literal["api"]` 扩展为 `Literal["api", "wecom"]`。

### 5.6 管理 API

```text
POST  /api/v1/apps/{app_id}/channels
      {"type":"api"}
      或 {"type":"wecom", "bot_id":"...", "secret":"...",
           "default_response":"...", "expires_at":null|"..."}

GET   /api/v1/apps/{app_id}/channels/wecom
PUT   /api/v1/apps/{app_id}/channels/wecom
      {bot_id?, secret?, default_response?, expires_at?, expected_config_revision}
PATCH /api/v1/apps/{app_id}/channels/wecom
      {enabled, expected_deployment_version}
POST  /api/v1/apps/{app_id}/deployments
      {"channel_type":"wecom", "release_id":"...", "expected_deployment_version":N}
POST  /api/v1/apps/{app_id}/channels/wecom/connect
GET   /api/v1/apps/{app_id}/deployments?channel_type=api|wecom
```

行为：

- 复用现有 `POST /deployments` 和 `ApplicationDeployRequest.channel_type`，把 `Literal["api"]` 扩展为 `Literal["api","wecom"]`；缺省值仍为 `api`，现有调用不变。渠道配置、启停和重连仍使用 `/channels/wecom` 子资源。
- 创建新版本继续使用现有 `POST /apps/{id}/releases`，它只创建不可变 release、不影响任何渠道；随后再用 `POST /deployments` 部署到选定渠道。现有 `POST /publish`（创建并部署到 API）保持原语义，不用于 WeCom。
- `PublicationChannelCreateRequest` 从只接受 `type="api"` 扩展为按 `type` 判别的联合；现有 `{"type":"api"}` 请求保持兼容。
- 创建 WeCom 渠道要求 `NIANLUN_INTERNAL_TOKEN` 已配置，否则返回 503；避免创建无法接入的配置。
- `bot_id` 长度 1–256，`default_response` 长度 1–1000（均去除首尾空白后校验）；`secret` 非空且不超过 512，不 trim。
- `expires_at` 必须是带时区的 ISO 8601，解析后转换为 UTC 存储；创建时省略或 `null` 表示不过期。`PUT` 中省略表示保持原值，显式 `null` 表示清除，传入新时间表示修改。
- `PUT` 中省略 `secret` 表示保持原值；显式传入新 secret 表示轮换。空字符串非法。
- `PUT` 校验 `bot_id` 未被其他应用占用。
- 所有创建/配置/启停变更都通过 5.1.1 分配新的 `config_revision`；`PUT` 成功后状态置 `offline`，best-effort 通知网关刷新。
- `PATCH enabled=true` 要求已有 active release、依赖可用且渠道未过期；校验通过后 `deployment_version += 1`，通过 5.1.1 分配新的 `config_revision`，并写 `action='enable'` 的 WeCom 事件。
- `PATCH enabled=false` 递增 `deployment_version` 并分配新的 `config_revision`，状态置 `offline`，并写 `action='disable'` 的 WeCom 事件。
- 部署校验与 API 渠道一致：版本存在、发布配置可用、知识库 snapshot 就绪、模型凭据可用；已过期渠道拒绝部署新版本并返回 409。
- 首次部署自动启用，并通过 5.1.1 分配新的 `config_revision`（启停状态变化）；已启用渠道部署新版本不改变连接，只影响新会话，`config_revision` 不变。
- 部署成功后写 `channel_type='wecom'`、`action='deploy'` 的部署事件，best-effort 通知网关刷新。
- `connect` 由平台调用已配置的网关控制地址，等待最多 12 秒（网关内部建连预算 10 秒），返回聚合状态；未配置控制地址返回 503，渠道停用、未部署或已过期返回 409。
- 所有写操作在 `ApplicationReleaseService.entry_lock(app_id)` 下执行；`expected_*` 不匹配返回 409，不覆盖并发修改。

### 5.7 内部 API

挂载在 `/internal/v1`，`include_in_schema=False`，不经过公开 API 中间件语义。

```text
GET  /internal/v1/wecom/snapshot
POST /internal/v1/wecom/channels/{channel_id}/status
POST /internal/v1/wecom/channels/{channel_id}/runs
```

鉴权：

- `Authorization: Bearer <NIANLUN_INTERNAL_TOKEN>`。
- 对 SHA-256 digest 做 `hmac.compare_digest`，避免长度侧信道。
- token 未配置时返回 503；错误 token 返回 401。
- 响应设置 `Cache-Control: no-store`。

### 5.8 内部运行与会话解析

`POST /runs` 请求体：

```json
{
  "revision": 3,
  "message": {
    "user_key": "wc_<sha256>",
    "text": "用户问题"
  }
}
```

原始 `msgid` 不传给平台；幂等键通过 `Idempotency-Key` 头传递，平台生成的 `message_id` 为内部 UUID。

处理步骤：

1. 路由先完成 token 校验。随后在 `ApplicationReleaseService.entry_lock(application_id)` 内检查 channel 存在、`enabled=true`、`expires_at` 未到期、`revision == config_revision`。不存在返回 404，停用或过期返回 403，revision 不匹配返回 409。
2. 校验 `text` 非空、UTF-8 字节数不超过 64 KiB（与网关一致）。
3. 仍在同一 entry lock 内处理幂等和会话解析：
   - 先按 `(application_id, client_id, user_key, idempotency_key)` 查已有 run：
     - 指纹不同：409。
     - 指纹相同且 `status='completed'`：204（首次投递不回复；网关重试时按 6.7 用兜底文案结束）。
     - 指纹相同且 `status` 为 `running|cancelling`：409“会话正忙”。
     - 指纹相同且为其他终态（`failed|cancelled|timed_out|interrupted`）：409 带安全失败文案。这样网关在流中断后用同一幂等键重试时能拿到明确失败并发送兜底回复，而不是因 204 静默悬挂。
   - 查最新 `channel='wecom' AND external_user_id=user_key AND status='active'` 的会话。
   - 无会话：使用当前 active release，并在内存生成新 `conversation_id`；会话行与 run 在同一个 admit 事务中插入，runtime 构建失败不会留下空会话。
   - 有会话且 `context_state='ready'`、未过期、checkpoint 存在、旧版本可用、无活跃 run：继续使用，版本固定为会话的 `release_id`。
   - 有会话但上下文中断/过期/checkpoint 缺失/旧版本不可用：立即将旧会话 `context_state` 置为 `interrupted`、`status` 置为 `archived`，再按“无会话”方式准备新会话。
   - 有活跃 run：返回 409“会话正忙”。
   - 会话解析与 run 行插入必须在同一个 entry lock 临界区内完成；运行行上的唯一约束和“同一会话最多一个活跃 run”索引作为最终防线，并发重复受理返回 409 而不是创建第二个会话。
4. 校验选中的 release 与当前程序兼容、模型和知识库可用。与现有 API `prepare` 一致，runtime 构建仍在 `entry_lock` 内、但在进程级 `_guard` 外执行：同一应用的写操作会串行等待，其他应用不受影响。
5. 仍在同一 `entry_lock` 内，通过新增的 `ApplicationRunRepository.admit_channel(...)` 在单个写事务中再次校验渠道 `enabled=true`、未过期、revision 匹配（新会话还要求选中的 release 仍是当前 active release）；已有会话还必须匹配 `channel='wecom'`、`external_user_id=user_key`、`release_id`。随后插入新会话（如需要）、`begin_turn(channel='wecom')` 和 run，然后提交 executor。校验失败返回 403/409，不创建会话或 run；runtime 构建失败也不会留下空会话。
6. 返回 NDJSON。

管理写操作和内部运行受理共用同一个进程内 `entry_lock`。平台仍是单 API Server 进程，因此该锁能覆盖两者；若未来引入多 API 进程，必须改为数据库或分布式锁，不能假设当前锁跨进程生效。

`prepare_channel` 以路由开始受理的时间作为超时起点，`wait` 的超时覆盖 runtime 构建和 Agent 执行；构建完成后若已超过 `NIANLUN_WECOM_RUN_TIMEOUT_SECONDS`，直接返回 504，不再创建会话或 run。

会话身份：

- 单聊：`conversation_key = "single:" + userid`。
- 群聊：`conversation_key = "group:" + chatid`。
- 网关计算 `user_key = "wc_" + sha256(channel_id + "|" + conversation_key)[:32]`。
- 群内所有成员得到相同 `user_key`，因此共享同一段会话。
- 原始 userid/chatid 不进入平台数据库、日志或运行记录。

会话保留：

- `NIANLUN_WECOM_CONVERSATION_RETENTION_DAYS` 默认 30 天。
- 每次受理在 admit 事务里把 `expires_at` 设为 `now + retention`；`begin_turn` 更新 `last_message_at` 和 `updated_at`。
- run 完成时用同一保留期续期；`_execute` 不能固定使用 API 保留期。
- 过期的旧会话保留历史，不参与新消息解析。

Checkpoint：

- 新增 `checkpoint_thread_id(channel, conversation_id)`，返回 `f"{channel}:{conversation_id}"`；`channel` 只允许 `api`、`wecom`。
- API 运行线程继续使用 `api:<conversation_id>`；WeCom 运行使用 `wecom:<conversation_id>`。
- `ApplicationRunRepository.checkpoint_cleanup_candidates()` 和 `conversation_ids_for_application()` 返回 `list[tuple[conversation_id, channel]]`，查询包含 `channel IN ('api','wecom')`。
- `recover(checkpoint_missing=True)` 同时中断 `api` 和 `wecom` 会话，不能只处理 API。
- `delete_application` 和 `cleanup_expired_checkpoints` 用会话实际 channel 调用 `checkpoint_thread_id`，不再硬编码 `api:`。
- `begin_turn` 接收并校验会话 channel；`complete_turn`、`fail_turn` 通过 `conversation_id` 定位会话，不需要新增 channel 参数。

执行服务调整：

- `ApplicationInvocationService` 新增 `prepare_channel`、`stream_channel`，内部复用现有 `_execute`、`wait`、`PreparedInvocation.deltas`、运行记录和资源回收。
- `PreparedInvocation` 增加 `channel`、`timeout_seconds` 和 `retention_days`；`_execute` 用 `checkpoint_thread_id(prepared.channel, conversation_id)`，包括临时 messages 的 checkpoint 清理，不再硬编码 `api:`。
- `wait(prepared)` 改用 `prepared.timeout_seconds`，不再固定读取 `application_api_timeout_seconds`；API 路径注入原值，WeCom 路径注入 `NIANLUN_WECOM_RUN_TIMEOUT_SECONDS`。`_execute` 完成时传 `prepared.retention_days`，避免 WeCom 会话完成后被写回 API 的保留期。
- `checkpointer` 初始化条件改为 `application_api_enabled or internal_token is not None`，保证只启用 WeCom 时也能运行；初始化时的 `recover(checkpoint_missing=...)` 覆盖两个渠道。
- WeCom 运行使用独立的全局准入计数 `NIANLUN_WECOM_MAX_CONCURRENT`（默认 32），不消耗公开 API 的 per-client RPM 限额。`_active` 记录每个运行的 channel；API 限额只统计 `channel='api'`，WeCom 只检查自己的全局计数，且不写入 `_rates`。
- executor 容量取 `NIANLUN_API_APPLICATION_MAX_CONCURRENT + NIANLUN_WECOM_MAX_CONCURRENT`，两个渠道同时满载时不会互相排队导致等待超时；只启用 WeCom 时也不会被 API 默认值卡住。
- WeCom 运行超时默认 120 秒；超时标记 `timed_out`。流尚未开始时返回普通 504，已开始后发送 `error` 事件（`code=504`）并结束。
- 网关侧运行超时应大于平台超时，让平台先返回明确的 504。
- NDJSON 客户端断开时，生成器 `finally` 取消 run。

### 5.9 状态与生命周期

- 网关每次状态上报携带 `revision`。
- 平台在同一个写事务内读取 `config_revision`、比较 revision 并更新状态，避免与配置变更并发时把旧 revision 的 online 写入新配置。
- 平台只接受 `revision == config_revision` 的上报；旧 revision 返回 204 并忽略。
- `online` 清空 `error_code`；`error` 必须携带允许的错误码；`offline` 清空 `error_code`。
- 落库的 `error_code` 只允许 `AUTH_FAILED | NETWORK_ERROR | CONNECTION_CONFLICT`。网关内部其他状态按 7.2 的映射处理，不进入平台状态字段。
- `last_reported_at` 在每次被接受的上报时更新，包括在线心跳；被 revision 或到期规则忽略的上报不更新。
- 管理台在 `last_reported_at` 超过 90 秒时显示“状态未知”。
- 停用渠道立即置 `offline`；配置变更立即置 `offline`，等待新 revision 的连接上报。
- 渠道已过期（`expires_at` 早于当前时间）时，平台只接受 `offline` 上报；`online`/`error` 上报返回 204 并忽略，避免网关时钟或旧连接把过期渠道改回在线。`enabled` 不变，`expires_at` 延后或清空后 `config_revision` 变化，网关重新建连。
- 首次部署自动启用时，`expires_at` 若已过期则拒绝启用并返回 409。
- 模型凭据或知识库快照失效不会自动断开长连接；新运行返回 403/503，管理台通过 `dependency_error` 提示，网关使用默认回复。
- 网关侧只有持有有效所有权的实例才上报状态；失去租约后立即停止上报，避免覆盖新 owner。

### 5.10 发布资源与删除保护

- `live_release_ids()`：
  - 现有会话查询从 `channel == 'api'` 改为 `channel IN ('api','wecom')`。
  - 增加已启用 WeCom 渠道的 `active_release_id`。
  - 运行中的 run 已覆盖两个渠道。
- `assert_application_deletable()`：
  - WeCom 渠道启用时阻止删除。
  - 存在未过期 WeCom 会话时阻止删除；未过期 API 会话检查保留。
  - 现有运行中 run 检查继续生效，错误文案按渠道区分。
  - 未过期会话查询同样改为 `channel IN ('api','wecom')`。
- 停用且无会话的 WeCom 配置随应用级联删除；`application_wecom_bot_revisions` 不随应用删除。

### 5.11 网关控制客户端

- 平台配置 `NIANLUN_GATEWAY_CONTROL_URLS`（逗号分隔）和 `NIANLUN_GATEWAY_CONTROL_TOKEN`。
- 配置变更/启停/部署后向所有控制地址发送 `POST /api/channels/refresh`，2 秒超时，失败只记日志。
- 手动重连向所有控制地址发送 `POST /api/channels/connect`，等待最多 12 秒，返回任一实例的成功状态；全部失败返回 502。
- 多实例下平台不保存网关实例列表；控制地址是运维配置，轮询快照始终兜底。

### 5.12 会话查询

现有对话查询只返回 `console` 会话。WeCom 会话需要可在管理台查看：

- `GET /api/v1/apps/{app_id}/conversations` 增加可选 `channel` 查询参数，取值 `console|api|wecom`，默认 `console`，保持现有前端行为。
- `SQLiteChatRepository.list_conversations` 接收 `channel` 并过滤；不再硬编码 `console`。
- `GET /api/v1/apps/{app_id}/conversations/{conversation_id}/messages` 和消息来源读取按会话实际 channel 读取，不限定 console；仍校验应用归属。`get_messages`、`get_source` 不能再附带 `channel == 'console'` 条件。
- `ConversationResponse` 增加 `channel` 和 `external_user_id` 字段，前端类型同步；`external_user_id` 对 WeCom 是 `user_key` 哈希值。
- WeCom 会话展示 `title`、消息数、最后消息时间和 `user_key`（哈希值），不显示原始企微 ID。
- 删除会话仍只允许 `console`；WeCom 会话由保留策略清理，避免误删活跃 checkpoint。
- 管理台在应用详情中增加会话渠道筛选；默认仍显示 console。

## 6. 网关设计

### 6.1 包结构

```text
app/channel_gateway/
  __init__.py
  __main__.py            # python -m app.channel_gateway
  config.py              # GatewaySettings.from_env()
  protocol.py            # Pydantic 契约、限制、错误类型
  server.py              # FastAPI 控制面
  manager.py             # ChannelManager、快照 diff、消息调度
  message.py             # 帧校验、文本归一化、user_key、截断
  wecom_adapter.py       # 官方 SDK 适配层
  clients.py             # 平台快照/状态/NDJSON 客户端
  ownership.py           # OwnershipManager 协议 + LocalOwnershipManager
  redis_ownership.py     # RedisOwnershipManager
  redis_scripts.py       # 从 TS 版移植的 Lua 脚本
  metrics.py             # 进程内计数器
tests/channel_gateway/
  ...
```

- 网关不导入 `app.api_server` 或平台 ORM。
- 仅依赖标准库、FastAPI/uvicorn、httpx、redis、官方 SDK。

### 6.2 控制接口

```text
POST /api/channels/connect
     {"channel_id": "...", "revision": 3, "request_id": "..."}
POST /api/channels/refresh
GET  /health
GET  /ready
GET  /internal/metrics
```

- 控制接口使用 `NIANLUN_GATEWAY_CONTROL_TOKEN`，常量时间比较。
- `connect` 先同步拉取一次快照（与现有快照请求相同的大小和校验限制），再按最新快照对目标渠道执行 ownership `recover` 并等待建立连接。配置已停用、渠道已过期、revision 不匹配或网关 10 秒建连预算内未成功返回 409/502。快照拉取失败时使用上一版快照并记录日志，不阻塞手动重连。
- `refresh` 异步触发一次快照拉取，立即返回 202。
- `/health` 只表示进程存活。
- `/ready` 表示快照至少成功加载过一次，且所有权后端可用；Redis 后端不可用时返回 503。
- `/internal/metrics` 返回连接数、消息数、去重数、回复成功/失败数、活跃回复数、重连数；不返回消息内容或凭据。
- 默认绑定 `127.0.0.1:3100`。

### 6.3 快照与 diff

快照来源：

```json
{
  "channels": [
    {
      "channel_id": "uuid",
      "application_id": "app-...",
      "revision": 3,
      "enabled": true,
      "connection": {"bot_id": "...", "secret": "..."},
      "runtime": {
        "default_response": "...",
        "expires_at": null
      }
    }
  ]
}
```

- 启动时拉取，之后每 30 秒拉取；控制 `refresh` 立即拉取。
- 平台只返回 `enabled=true` 且 `active_release_id` 非空的渠道；停用、未部署或删除的渠道从快照中消失。已启用但已过期的渠道仍保留在快照中并带 `expires_at`：持有租约的实例据此断开并上报 `offline`；启动时没有对应 entry 的实例直接跳过，不竞争租约、不建连、也不上报，平台以 `expires_at` 显示“已过期”。
- 网关启动时若发现 `expires_at` 已过期，不建连；连接中到期由本地定时器触发断开，不依赖平台再推送。
- 校验 `channel_id`、`bot_id` 全局唯一；revision 为正整数；secret 非空；`expires_at` 为可解析的带时区时间或 `null`。
- 快照非法时保留上一版快照和现有连接，记录安全日志。
- diff 规则：
  - 新启用渠道：竞争所有权并建连。
  - revision 变化：断开旧连接、释放旧租约、按新 revision 重新建连。
  - 配置不变：继续使用现有连接。
  - 快照中消失：断开、释放租约。
- 快照请求失败不清空现有连接；平台短暂不可用时机器人继续在线，消息运行失败由默认回复兜底。

### 6.4 所有权：local 与 redis

统一接口（与 TS 版语义一致）：

```text
acquire(input) -> Lease | None
renew(lease) -> bool
release(lease) -> None
block(lease) -> bool
recover(input) -> bool
has_recovered(lease) -> bool
publish_status(lease, status, error_code) -> State | None
read_status(input) -> State | None
is_valid(lease) -> bool
close() -> None
```

`Lease` 字段：`bot_id`、`channel_id`、`revision`、`recovery_version`、`token`、`generation`、`safe_until`。

**LocalOwnershipManager**

- 单进程内存实现，行为与 Redis 版一致，用于单机默认和测试。
- 保存 owner、binding、generation、conflict、recovery version、expiredRevision、status。
- 没有跨进程 fencing；文档明确单机模式只能运行一个网关进程。

**RedisOwnershipManager**

- 使用 `redis.asyncio.Redis`。
- 从 TS 版 `src/ownership/redis.ts` 直接移植 7 个 Lua 脚本：acquire、block、recover、renew、release、publish_status、read_status。
- key 使用 bot_id 的 sha256 hash tag，兼容 Redis Cluster：

```text
GATEWAY_REDIS_KEY_PREFIX:{<sha256(bot_id)>}:owner
...:generation
...:conflict
...:binding
...:recovery-version
...:recovery-requests
...:status
```

- `GATEWAY_REDIS_KEY_PREFIX` 默认 `nianlun:channel-gateway`。若要与已有 TS 网关共用租约和状态，设置为 TS 的默认前缀 `channel-gateway`；不做隐式兼容。
- 租约 TTL 默认 30 秒，安全窗口 5 秒；续租间隔取 10 秒、TTL 的三分之一和 `(TTL - safety) / 2` 中的最小值，保证安全余量较大时仍在安全期限前续租。Redis 连接和读取超时也不得超过安全期限的一半。
- 每个渠道独立计时续租，Redis 调用耗时计入间隔；慢渠道不阻塞其他租约，移除渠道时取消并等待其续租任务。Redis URL 查询参数不能覆盖计算出的连接和读取超时。
- `acquire` 原子校验 revision/binding，冲突 revision 未推进时拒绝重新占用，避免反复抢连。
- `renew` 同时校验 token、binding revision、generation 和 recovery version；失败立即断开。
- `publish_status` 只在租约有效且剩余 TTL 大于安全窗口时写入。
- `block` 只在 `CONNECTION_CONFLICT` 时登记冲突 revision；释放租约不会清除冲突。
- 冲突先在本地生效；登记失败后由协调流程重试，正在执行的登记任务合并复用，已结束的失败任务不得阻止后续重试。
- `publish_status` 收到 `CHANNEL_EXPIRED` 时写入 binding 的 `expiredRevision`（与 TS 脚本一致），随后释放租约即可，不调用 `block`；同一 revision 即使清理了本地状态也不能重新占用，`acquire`、`recover` 和 `publish_status` 都拒绝不高于 `expiredRevision` 的版本，只有更高 revision 才能恢复。
- `read_status` 对 `CHANNEL_EXPIRED`、`CONNECTION_CONFLICT`、`GATEWAY_STOPPED` 这些终态即使当前没有有效 owner 也返回；其他状态必须满足租约有效且剩余 TTL 大于安全窗口，避免读取旧 owner 的过期状态。认证失败会保持租约重试，不需要额外的终态读取。
- 状态码到平台的映射见 7.2；`CHANNEL_EXPIRED` 不是公开错误码，映射为 `offline`，不能归类为 `AUTH_FAILED`。
- `recover` 用于手动重连：只允许在已登记冲突或 owner 失效时推进 recovery version，不能抢占有效租约。渠道已过期时 `recover` 也必须拒绝。
- Redis 状态用于网关控制面跨实例读取现有 owner 状态；平台仍是管理台状态的来源。
- Redis 不可用时：
  - 启动阶段 `/ready=false`，不建连。
  - 运行阶段续租失败立即断开对应渠道连接并停止上报，恢复后重新竞争，绝不降级到 local 造成双连。
- 配置：

```text
GATEWAY_OWNERSHIP_BACKEND=local|redis   # 单机默认 local
REDIS_URL=redis://127.0.0.1:6379/0
GATEWAY_REDIS_KEY_PREFIX=nianlun:channel-gateway
GATEWAY_LEASE_TTL_MS=30000
GATEWAY_LEASE_SAFETY_MS=5000
```

- Redis 只用于所有权/栅栏/跨实例状态，不用于消息去重；每个 Bot 同时只有一个 owner，本地去重足够。

### 6.5 连接生命周期

1. 快照出现启用渠道，ChannelManager 检查 `expires_at`。已过期则不竞争租约、不建连；未过期则注册到期定时器并竞争租约。
2. 租约成功后才创建 `WecomAdapter` 并连接。
3. 适配层创建新的 `WSClient`，使用静默 logger、`max_reconnect_attempts=0`，等待 `authenticated` 或失败事件，连接超时 10 秒。
4. `authenticated` 后上报 `online`，开始接收消息。适配层同时监督 SDK 连接存活状态，补偿 SDK 在 WebSocket 正常关闭时未发出 `disconnected` 的情况；手动关闭和替换连接时清理监督任务，旧连接不得影响新连接。
5. `disconnected`/`error`：
   - 认证失败或订阅失效：上报 `AUTH_FAILED`（`status=error`），取消在途运行并断开 SDK，保持租约按固定 60 秒退避重试；续租失败时释放租约并重新竞争。
   - 网络错误：上报 `NETWORK_ERROR`（`status=error`），取消在途运行并断开 SDK，保持租约按 1s→30s 指数退避重试；续租失败时释放租约并重新竞争。
   - `event.disconnected_event` 或企微冲突错误码：`block` 登记冲突，上报 `CONNECTION_CONFLICT`，取消在途运行、释放租约并停止自动重试，等待恢复版本推进或配置 revision 变化。
6. `expires_at` 到达：
   - 立即标记渠道为本地过期，取消所有在途运行，断开连接并停止回复。
   - 先 `publish_status(CHANNEL_EXPIRED)` 写入 ownership 终态和 `expiredRevision`（平台侧映射为 `offline`），再释放租约；不调用 `block`。
   - 不再自动重连，直到快照出现更高的 `config_revision`（例如延后到期或清空 `expires_at`）。
7. 配置 revision 变化、渠道停用、快照删除、所有权丢失、Redis 租约丢失：立即断开、取消在途运行并释放/阻止租约。
8. 进程关闭：停止快照、best-effort 上报 `offline`（内部码 `GATEWAY_STOPPED`，仍持有租约时）、断开所有连接、释放租约、关闭 Redis 和 HTTP 客户端；失去租约或上报失败时只记录日志。

重连策略由 ChannelManager 控制，每次重连前重新检查 ownership；SDK 内置重连关闭。ownership 写入失败不能回退为继续连接：冲突和到期状态先在本地生效，同一 revision 不自动重连，避免 Redis 短暂不可用时形成双连。

### 6.6 消息归一化

适配层订阅通用 `message` 事件并按 `msgtype` 分支，不能只订阅 `message.text`，否则图片、语音等消息无法回复“仅支持文本”的提示。

只接受：

```text
frame.cmd == "aibot_msg_callback"
frame.headers.req_id 非空
frame.body.aibotid == 配置 bot_id
frame.body.msgid 非空
frame.body.chattype ∈ {single, group}
frame.body.from.userid 非空
frame.body.chattype == "group" 时 frame.body.chatid 非空
frame.body.msgtype == "text"
frame.body.text.content 非空且去空白后非空
```

派生：

```text
req_id           = frame.headers.req_id             # 仅用于回复，不落库
conversation_key = "single:" + userid  或  "group:" + chatid
user_key         = "wc_" + sha256(channel_id + "|" + conversation_key)[:32]
event_id         = sha256(channel_id + "|" + msgid)
idempotency_key  = "wcm_" + sha256(channel_id + "|" + msgid)
```

限制：

- 文本 UTF-8 字节数 ≤ 64 KiB。
- 超过限制回复“消息内容无效或超出限制，请检查后重试”。
- 非文本消息回复“当前仅支持文本消息”。
- 缺少 msgid、chatid、bot 不匹配等非法帧静默丢弃并记录计数。
- `event_id` 使用 10k/5min LRU 去重；处理中消息标记为 pending，完成后保留 TTL。
- 重复帧不回消息。

### 6.7 并发与回复

- 每 `user_key` 同时只处理一个请求；忙时立即回复“上一条消息还在处理中，请稍后再试”。
- 全局并发默认 32，可配置；达到上限时同样立即回复忙提示，不调用平台。
- 收到合法消息后立即生成 `stream_id`，但暂不发送内容；平台返回 `start` 后发送一次“正在思考中...”，随后按 300ms 节流调用 `reply_stream`。这样重复投递在 `start` 之前收到 204 时不会留下无法结束的占位消息。
- `GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS`（默认 30 秒）内未收到 `start`：取消平台流并直接发送默认回复终态，避免首包长时间无响应。
- 15 秒无新 chunk 时发送一次当前内容作为保活。
- `done` 后用完整 answer 截断到 20 KiB，`finish=True` 提交最终结果；截断必须按 UTF-8 字节边界，不能产生半个字符。`done.content` 去空白后为空按失败处理，发送默认回复。
- 平台错误：
  - 409 busy：忙提示。
  - 409 stale revision：刷新快照，若渠道仍启用则重试一次（首次请求未被受理）；否则默认回复。
  - 204 duplicate：首次投递不回复；重试时一律用兜底文案发送终态。上一轮发送过占位或内容时用它关闭原流，上一轮未发送过任何帧时用它避免用户完全收不到回复。
  - 404：刷新快照并从本地移除该渠道（渠道已删除），不重试。
  - 401：内部 token 配置错误，默认回复并记录安全日志，不重试。
  - 403：刷新快照；渠道停用或过期时断开，发送默认回复；其他 403 也默认回复，不重试。
  - 429/5xx/首次请求网络失败：默认回复，不重试；NDJSON 流已开始后中断按下面的重试规则处理。
- 已发送真实增量内容后失败：以“已输出内容 + 回答未完成提示”结束；只发送过占位消息或未发送内容时直接以默认回复结束。
- 单帧和终态回复都按 20 KiB 截断；内容超过上限后不再发送中间帧，但继续读取平台流直到 `done`，再用截断后的完整 answer 发送终态。不能因为截断而取消平台流，否则 run 会被标记取消、会话上下文被中断。
- 累计 answer 或最终 `done.content` 超过 1 MiB 时停止读取并取消平台流，按 `CHANNEL_ANSWER_TOO_LARGE` 记录指标并视为失败：尚未尝试终态时发送默认回复，已尝试终态时只记录失败。不能把超限内容当作成功回答结束。
- NDJSON 单行上限按 `GATEWAY_MAX_ANSWER_BYTES * 6 + 16 KiB` 计算，超过上限视为协议错误并取消流，避免恶意或异常响应耗尽内存。
- 平台流超过 `GATEWAY_WORKFLOW_TIMEOUT_SECONDS` 未结束：取消 HTTP 流；已有内容则加“回答未完成提示”结束，无内容则默认回复。
- 平台流在终态事件前意外结束：本次尝试没有回复发送失败、且未尝试终态回复时，可用同一 `Idempotency-Key` 重试，最多 3 次尝试；任何中间帧或终态 ACK 失败、终态结果未知时不重试。
- 重试收到 204（原 run 已完成）时一律用兜底文案发送终态：上一轮发送过占位或内容时关闭原流，上一轮未发送过任何帧时避免用户完全收不到回复；重试收到 409 终态失败时按失败处理并发送兜底终态。
- 仅当状态码和错误码可确认回复未被企微接受时才允许重试；ACK 超时视为结果未知，不重试。

### 6.8 日志与指标

- 使用标准结构化日志。
- 记录：channel_id、revision、event_id 哈希、user_key 哈希、耗时、状态、错误码、provider errcode。
- 不记录：消息正文、bot secret、完整帧、原始 userid/chatid、Authorization。
- 适配层只记录异常类型、错误码和 errcode，不原样输出 SDK 异常文本；认证失败和连接错误不得包含可能带凭据的 URL 或请求帧。
- SDK logger 静默，禁止默认 debug 输出。
- 指标：`connections`, `messages_received`, `messages_duplicate`, `messages_unsupported`, `replies_ok`, `replies_failed`, `replies_active`, `reconnects`, `lease_acquire_failed`, `workflow_errors`。

## 7. 内部契约

### 7.1 快照

```json
{
  "channels": [
    {
      "channel_id": "uuid",
      "application_id": "app-...",
      "revision": 3,
      "enabled": true,
      "connection": {"bot_id": "...", "secret": "..."},
      "runtime": {
        "default_response": "...",
        "expires_at": null
      }
    }
  ]
}
```

- `secret` 只在此内部接口出现。
- `Cache-Control: no-store`。
- 平台只返回 `enabled=true` 且 `active_release_id` 非空的渠道；停用或未部署渠道的 secret 不出现在快照中。
- 已启用但已过期的渠道仍返回，并带 `expires_at`；持有租约的网关据此断开并保持 `offline`，新启动实例跳过且不建连，避免把到期误判为删除后重新连接。
- 快照大小限制 1 MiB，超过则返回 500 并记录日志。

### 7.2 状态上报

```json
{
  "revision": 3,
  "status": "online",
  "error_code": null
}
```

- `status`: `offline | online | error`。
- `error_code`: `AUTH_FAILED | NETWORK_ERROR | CONNECTION_CONFLICT | null`。
- 网关内部状态到上报字段的映射：

| 内部状态/错误码 | 上报 `status` | 上报 `error_code` | 说明 |
| --- | --- | --- | --- |
| `online` | `online` | `null` | 已认证并接收消息 |
| `AUTH_FAILED` | `error` | `AUTH_FAILED` | 凭据或订阅失效 |
| `NETWORK_ERROR` | `error` | `NETWORK_ERROR` | 网络或心跳失败 |
| `CONNECTION_CONFLICT` | `error` | `CONNECTION_CONFLICT` | Bot 被其他连接踢下线 |
| `CONNECTING` | `offline` | `null` | 建连中 |
| `CHANNEL_EXPIRED` | `offline` | `null` | 渠道到期 |
| `GATEWAY_STOPPED` | `offline` | `null` | 网关正常停止 |
| `LEASE_UNAVAILABLE` | 不上报 | — | 操作失败不覆盖平台运行状态 |
| `CONFIG_UNAVAILABLE` | 不上报 | — | 同上 |
| `CONNECT_TIMEOUT` | 不上报 | — | 同上 |

- 成功返回 204。
- revision 不匹配返回 204，平台忽略。
- 渠道已过期时，平台只接受 `offline`；`online`/`error` 返回 204 并忽略。
- `last_reported_at` 只在被接受的上报时更新；在线状态下网关每 30 秒上报一次保活。

### 7.3 运行

请求头：

```text
Authorization: Bearer <NIANLUN_INTERNAL_TOKEN>
Idempotency-Key: wcm_<sha256>
Content-Type: application/json
```

请求体：

```json
{
  "revision": 3,
  "message": {
    "user_key": "wc_<sha256>",
    "text": "用户问题"
  }
}
```

成功响应 `application/x-ndjson`，并设置 `Cache-Control: no-store`、`X-Accel-Buffering: no`：

```json
{"type":"start","run_id":"...","conversation_id":"...","release_id":"..."}
{"type":"chunk","content":"增量文本"}
{"type":"done","content":"完整回答","run_id":"..."}
{"type":"error","code":502,"message":"模型调用失败"}
```

- 每行一个 JSON，UTF-8，`ensure_ascii=false`，行尾 `\n`。
- `chunk.content` 为增量文本，可能包含换行。
- `done.content` 为完整 answer；网关以它为准做最终截断。
- 请求和事件均使用严格 schema，拒绝额外字段；`/runs` 请求体上限 512 KiB（64 KiB 文本最坏 JSON 转义约 384 KiB，加协议开销），超限返回 413。
- `error` 只返回安全文案，不含模型请求、异常堆栈或凭据。
- 流开始前错误返回普通 JSON envelope 和 HTTP 状态码：
  - 401 内部 token 无效
  - 403 渠道停用、渠道已过期或应用未发布
  - 404 渠道不存在
  - 409 revision 过期、会话忙、幂等键冲突或幂等键对应的 run 已失败终止
  - 429 平台并发超限
  - 504 运行超时
- 重复运行（相同 `Idempotency-Key` 且指纹相同）：
  - 已有 `completed` run：返回 204；首次投递不回复，网关重试时按 6.7 发送兜底终态。
  - 已有 `running|cancelling` run：返回 409 “会话正忙”。
  - 已有其他终态 run（`failed|cancelled|timed_out|interrupted`）：返回 409 和安全失败文案，让网关发送兜底回复。
  - 指纹不同：返回 409。
- 客户端断开：平台取消 run 并释放资源。

## 8. 前端设计

### 8.1 渠道列表

应用总列表分别展示 API 与企微渠道的发布状态；只有两者均无发布版本时才显示“未发布”。企微到期状态在列表和发布弹窗内独立计时更新，提交发布前再次检查当前时间，过期时不创建版本。

`ApplicationDetail` 的渠道表支持多行：

- API 服务行：保持现有行为。
- 企微机器人行：
  - 状态徽标：在线/离线/异常/已过期/状态未知。
  - 错误提示：认证失败、网络异常、连接冲突。
  - 运行版本：`rN` 或“未发布”。
  - 到期时间；已过期时禁用发布、启用和重连，只保留编辑配置。
  - 最后上报时间。
  - 操作：发布版本、启用/停用、重连、编辑配置和发布记录。发布记录可直接从企微渠道行进入，不依赖创建 API 渠道。
- 渠道计数为已创建渠道数量。
- “创建发布渠道”弹窗先选择类型：
  - API：现有流程。
  - 企微：bot_id、secret、默认回复；secret 不回填。

### 8.2 发布弹窗

- “目标渠道”下拉支持 `API 服务` 和 `企微机器人`。
- 版本列表和“最新已保存草稿 → 创建新版本”保持共享；创建新版本调用现有 `POST /releases`，不改变任何渠道的 active release。
- 比较目标版本时使用所选渠道的 active release。
- 提交部署时统一调用 `POST /deployments`：
  - API：`channel_type="api"`，沿用现有行为。
  - WeCom：`channel_type="wecom"`，携带所选渠道的 `expected_deployment_version`。
- 未创建渠道、配置未保存、草稿未保存时禁用提交。
- 发布成功后刷新 publication，并展示新渠道状态。
- 版本管理和版本详情展示 API、企微各自的使用状态；同一版本可同时被两个渠道使用。只要还有未使用该版本且可发布的渠道，版本行仍允许发布，并默认选择这样的目标渠道。

### 8.3 配置弹窗

- 显示 bot_id、默认回复、到期时间、secret 状态（已配置/未配置）。
- 到期时间可清空；留空表示不过期。
- 修改 secret 时输入新值；留空保持原值。
- 保存携带 `expected_config_revision`；冲突时提示刷新。
- 提示：同一机器人只能由一个网关进程连接；多实例部署需要 Redis。

### 8.4 会话视图

- 应用详情的会话列表增加渠道筛选：控制台/API/企微；默认控制台。
- 企微会话行显示渠道徽标、`user_key` 摘要和最后消息时间。
- API 和企微消息只读展示，不提供删除；控制台会话保留删除操作。
- 切换会话或渠道时清理旧数据；请求失败或取消后不得把旧会话内容显示在新会话标题下。

### 8.5 类型与测试

- 更新 `app/frontend/src/types.ts`。
- 更新 `app/frontend/src/api/client.ts`。
- 更新 `ApplicationDetail.tsx`、`ApplicationPublishPanel.tsx`、渠道弹窗、部署历史和会话筛选。
- Vitest 覆盖创建、secret 掩码、状态展示、到期时间与已过期禁用、按渠道部署、重连、错误提示和会话渠道筛选。

## 9. 数据库迁移

迁移必须可重复执行，并覆盖已有数据库升级。

1. 新建 `application_wecom_channels` 和 `application_wecom_bot_revisions`。
2. 重建 `conversations`：
   - 不能复用 `_rebuild_conversations_api_constraints`，因为它只要看到三个旧 check 存在就会提前返回。
   - 保留现有 check 名称 `ck_conversations_channel`、`ck_conversations_api_owner`、`ck_conversations_context_state`，只改条件。否则旧 helper 每次启动都会误判缺失并重复重建表。
   - 新增 `_rebuild_conversations_channel_constraints`，以表 SQL 中是否已包含 `wecom` 作为幂等判断。
   - 读取旧表，创建新表并写入 `channel IN ('console','api','wecom')` 和新所有者约束。
   - 复制数据，删除旧表，重命名，重建索引。
3. 重建 `application_deployment_events`：
   - 新唯一键 `(application_id, channel_type, deployment_version)`。
   - 增加 `channel_type IN ('api','wecom')` check。
   - 复制已有数据，保持 `channel_type='api'`。
   - 以表 SQL 中是否已包含新的三列唯一键作为幂等判断，已满足时跳过重建。
4. 同步 `PublishingRepository` 的 `channel_type` 过滤；这是代码变更，不是迁移步骤但不做会导致 API 发布历史混入 WeCom 事件。
5. 将 `SCHEMA_VERSION` 提升到 13。`_REQUIRED_COLUMNS` 由 ORM metadata 自动派生，不需要手工改列表；但仍需确保 ORM model 包含两个新表和 `expires_at` 字段。
6. 运行 `PRAGMA foreign_key_check`；失败回滚。
7. 迁移测试使用旧 schema 的临时 SQLite，验证数据、约束、索引和外键。

注意：`initialize_database` 会先执行 `Base.metadata.create_all`，它只创建缺失表，不会修改已有表结构，因此第 2、3 步的重建仍然必需。SQLite 不能在开启外键时重建被引用表；沿用现有迁移的 `PRAGMA foreign_keys=OFF` + `BEGIN IMMEDIATE` 模式，并在 `finally` 恢复。

## 10. 配置

### 10.1 平台

```text
NIANLUN_INTERNAL_TOKEN=                     # 网关→平台；配置后才启用 WeCom 内部接口
NIANLUN_GATEWAY_CONTROL_URLS=               # 平台→网关，逗号分隔，可空
NIANLUN_GATEWAY_CONTROL_TOKEN=              # 平台→网关，配置时 ≥32 字符
NIANLUN_GATEWAY_CONTROL_TIMEOUT_SECONDS=2   # refresh 请求超时；connect 固定等待 12 秒
NIANLUN_WECOM_MAX_CONCURRENT=32
NIANLUN_WECOM_RUN_TIMEOUT_SECONDS=120
NIANLUN_WECOM_CONVERSATION_RETENTION_DAYS=30
```

校验：

- `NIANLUN_INTERNAL_TOKEN` 非空时长度 ≥32。
- 配置了 control URL 时必须配置 control token，且 token ≥32。
- 并发、超时和保留期必须大于 0。
- 平台数据库仍是 SQLite；不支持多 API Server 进程。

### 10.2 网关

```text
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=3100
GATEWAY_LOG_LEVEL=INFO

NIANLUN_API_URL=http://127.0.0.1:8000
NIANLUN_INTERNAL_TOKEN=
NIANLUN_GATEWAY_CONTROL_TOKEN=

GATEWAY_OWNERSHIP_BACKEND=local
REDIS_URL=
GATEWAY_REDIS_KEY_PREFIX=nianlun:channel-gateway
GATEWAY_LEASE_TTL_MS=30000
GATEWAY_LEASE_SAFETY_MS=5000

GATEWAY_SNAPSHOT_INTERVAL_SECONDS=30
GATEWAY_STATUS_HEARTBEAT_SECONDS=30
GATEWAY_CONNECT_TIMEOUT_SECONDS=10
GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS=30
GATEWAY_WORKFLOW_TIMEOUT_SECONDS=150
GATEWAY_MAX_CONCURRENT=32
GATEWAY_MAX_MESSAGE_BYTES=65536
GATEWAY_MAX_ANSWER_BYTES=1048576
GATEWAY_REPLY_MAX_BYTES=20480
GATEWAY_STREAM_INTERVAL_MS=300
GATEWAY_STREAM_KEEPALIVE_SECONDS=15
GATEWAY_DEDUP_MAX_ENTRIES=10000
GATEWAY_DEDUP_TTL_SECONDS=300
```

校验：

- internal token 和控制 token 必须非空且 ≥32 字符。
- `redis` 后端必须配置 `REDIS_URL`。
- `GATEWAY_REDIS_KEY_PREFIX` 非空且不能包含 `{`、`}`，避免破坏 hash tag。
- lease TTL 必须大于 safety 窗口。
- 并发、字节数、超时均为正数。
- `GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS` 应小于 `GATEWAY_WORKFLOW_TIMEOUT_SECONDS`。
- `GATEWAY_WORKFLOW_TIMEOUT_SECONDS` 应大于平台 `NIANLUN_WECOM_RUN_TIMEOUT_SECONDS`。

## 11. 安全

- Bot secret 只存在于平台 SQLite、内部快照和网关内存/连接配置中；网关不把快照或 secret 写入磁盘。
- 管理 API 永不返回 secret；日志、异常、指标、测试快照不得包含 secret。
- 内部接口使用专用 token，默认只绑定本机；反向代理不得暴露 `/internal`。
- 网关控制接口使用独立 token，默认只绑定本机。
- 消息正文不写入日志和指标；原始企微 userid/chatid 和 msgid 不写入平台数据库，用户身份统一使用 `user_key` 哈希。消息正文仅在平台会话/运行记录中按现有聊天记录策略持久化，供上下文和会话查看使用。
- 内部 NDJSON 只返回安全错误文案。
- SDK 使用静默 logger，避免官方 SDK 默认日志泄露帧和凭据。
- `REDIS_URL` 可能包含密码，日志和异常中必须脱敏。
- 多实例部署必须使用 Redis 所有权；禁止在无 Redis 的情况下运行多个网关连接同一 Bot。

## 12. 测试

### 12.1 平台

- 迁移：旧数据库升级、数据保留、约束和索引正确；新迁移 helper 在已有库上确实重建，不因旧 check 存在而跳过。
- 渠道创建/编辑/启停/部署；secret 不回显；`has_secret` 正确。
- `config_revision` 和 `deployment_version` 递增与乐观并发。
- 同一 `bot_id` 删除/重建、换绑后 revision 继续单调递增，不复用旧值。
- 部署、启用、停用分别写 `channel_type='wecom'` 事件；API 部署历史和 `last_deployed_at` 不被 WeCom 事件污染，默认查询仍为 API。
- `POST /releases` 只创建版本，不改变 API 或 WeCom 的 active release；只有 `POST /deployments` 改变所选渠道。
- 到期时间创建/修改/清除；到期后 `online` 上报被忽略、内部运行返回 403、`connect` 返回 409、快照仍包含该启用渠道并带过期时间。
- WeCom 渠道自己的 `has_unpublished_changes`、`dependencies_ready` 和 `last_deployed_at`。
- 单聊/群聊 user_key 会话隔离与群内共享；原始企微 ID 和 msgid 不落库。
- 同一 user_key 并发受理在 entry lock 下只创建一个会话和一个 run，另一个返回 409。
- 会话续用、过期、上下文中断、checkpoint 缺失自动新建，旧会话被置为 `interrupted`/`archived`。
- 重新部署后已有会话固定旧版本，新会话使用新版本。
- Checkpoint 前缀：WeCom 使用 `wecom:`、API 使用 `api:`；应用删除、过期清理和 checkpoint 缺失恢复同时覆盖两个渠道。
- 内部鉴权：无 token、错误 token、未配置 token。
- 快照只含启用渠道；包含 secret 但管理接口不包含。
- 状态上报 revision 过滤、在线保活、错误码、offline 清除错误码。
- NDJSON：start/chunk/done/error、`completed` 重复 204、失败终态重复 409、运行中重复 409、指纹冲突 409、停用 403、busy 409、并发 429、超时 504。
- 客户端断开取消 run。
- 删除保护和 live release 保留。
- 会话查询渠道筛选、WeCom 消息可读、WeCom 会话不可删除。
- 公开 API 关闭但 WeCom 启用时 checkpointer 正常工作，executor 容量覆盖两个渠道并发之和。
- WeCom 运行使用独立准入计数，不消耗公开 API RPM。
- API 与 WeCom 分别使用自己的超时；`wait` 不再固定读取 API 超时；runtime 构建耗时计入 WeCom 超时，构建超预算时不创建会话或 run。
- WeCom 完成时使用 WeCom 会话保留期，不会被 API 保留期覆盖。
- 首次部署自动启用会分配新的 `config_revision`，后续部署不改变 revision。

### 12.2 网关

- 帧校验、文本归一化、单聊/群聊 user_key、非法帧丢弃。
- 去重、忙提示、全局并发、超时、UTF-8 边界截断、默认回复。
- 快照 diff：新增、revision 变化、停用/删除后从快照消失、非法快照、启用但已过期；新启动实例跳过已过期渠道且不建连。
- 到期定时器：连接中断开、取消在途运行、写 `CHANNEL_EXPIRED` 终态、写 `expiredRevision`、不自动重连；更高 revision 延后到期后恢复。
- 认证失败保持租约并按 60 秒重试；网络错误保持租约并按 1s→30s 退避；续租失败才释放租约并重新竞争。
- 首事件超时发送默认回复；404 移除本地渠道；20 KiB 截断后继续读取平台流；重试收到 204/409 时的兜底终态，包括上一轮只发送过占位消息的情况。
- local ownership 语义。
- redis ownership：Lua 脚本、租约续期、冲突 block/recover、expired revision、终态读取、Redis 不可用；单元测试使用 fake，`REDIS_URL` 存在时运行集成测试。
- Redis key 前缀可配置；与 TS 共用前缀时不产生不一致的 key 结构。
- 官方 SDK 适配层 fake：
  - 认证成功/失败。
  - 网络断开、SDK 重连关闭、Manager 重连。
  - `event.disconnected_event` 冲突。
  - 回复 ACK errcode 846604–846609。
  - ACK 超时/异常格式。
- 状态上报只在有效租约内进行；关闭时 best-effort 上报 offline。
- 控制接口鉴权、`connect` 语义、`/ready`、metrics。
- NDJSON 解析：分块、超大行、错误事件、204、超时取消。

### 12.3 前端

- 渠道创建类型选择、企微表单、secret 不回填。
- 状态徽标、错误文案、状态未知。
- 按渠道发布和版本比较。
- 重连、启停、配置冲突提示。
- 会话渠道筛选和只读企微消息。

### 12.4 人工验收

真实企业微信环境验收：

1. 配置机器人，创建 WeCom 渠道并保存凭据。
2. 创建版本、首次部署，网关状态变为在线。
3. 单聊发送文本，收到流式回答；会话记录可在平台查看。
4. 群聊 @机器人，多个成员共享同一会话。
5. 重新部署版本，新会话使用新版本，旧会话继续旧版本。
6. 停用渠道，网关断开；重新启用后恢复。
7. 修改 secret，网关重连。
8. 设置到期时间并等待到期：网关断开、管理台显示已过期、不再自动重连；延后到期后恢复。
9. 单机 local 模式只运行一个网关；Redis 模式运行两个网关，只有一个获得所有权。
10. 断开网络/重启网关，状态和重连符合预期。
11. 检查管理响应、日志、指标中无 secret、消息正文和原始企微 ID。

## 13. 部署与运行

平台：

```bash
uv sync --all-groups
uv run python -m app.api_server.main
```

网关：

```bash
uv sync --all-groups
uv run python -m app.channel_gateway
```

单机模式：`GATEWAY_OWNERSHIP_BACKEND=local`，只启动一个网关。
多实例模式：启动多个网关，设置 `GATEWAY_OWNERSHIP_BACKEND=redis` 和同一个 `REDIS_URL`；平台 control URL 可配置多个实例，快照轮询兜底。

`.env.example` 增加上述变量并给出安全默认值；`docs/application_api.md` 或新增简短的企微接入文档说明机器人后台配置、启动命令和多实例约束。

## 14. 验收标准

- 平台可创建、配置、发布、启停企微渠道，发布版本与 API 渠道互不影响。
- 网关可连接/断开/重连企微机器人，并正确上报三种状态。
- 单聊和群聊文本消息端到端流式回答，群聊共享会话。
- 已有会话固定发布版本，新会话使用新版本。
- 渠道到期后不再建连，管理台显示已过期；延后到期后恢复。
- 同一 bot_id 重新配置时 revision 单调递增，旧租约和冲突不会阻塞新配置。
- 本地和 Redis 两种所有权后端均可用；Redis 多实例只有一个 owner。
- 管理接口、日志、指标不泄露 secret、消息正文和原始企微 ID。
- 旧数据库可无损升级。
- 定向测试、`make lint`、`make typecheck`、前端 `npm test`、`npm run typecheck`、`npm run build` 通过。

## 15. 风险与后续

| 风险 | 缓解 |
| --- | --- |
| 官方 Python SDK 较新，行为可能变化 | 锁版本、适配层隔离、契约测试、升级单独评审 |
| SDK `connect()` 不抛异常且 `_started` 粘滞 | 每次建连新建实例；等待 authenticated/error 超时 |
| SDK 回复 ACK 错误只有字符串 | 解析 errcode；格式不识别时归为 unknown，不重试 |
| Redis 不可用导致无法确认所有权 | 停止建连并断开，禁止降级 local，恢复后重新竞争 |
| 平台 SQLite 单写者 | 平台保持单进程；Redis 只扩展网关 |
| 多实例控制推送只覆盖部分实例 | 支持多个 control URL；30 秒快照轮询兜底 |
| 群聊共享上下文可能包含多人信息 | 产品预期行为；按群一个会话，文档明确 |
| 平台 runtime 构建慢导致首包延迟 | 构建计入 WeCom 超时；网关在 `GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS` 内未收到 `start` 即兜底回复 |
| 同一应用的 runtime 构建串行 | 与现有 API `prepare` 一致；`entry_lock` 保证会话/版本一致，v1 接受同应用构建串行 |
| secret 明文存 SQLite | 与现有模型凭据一致；后续可引入静态加密 |
| 企微回复长度限制变化 | 20 KiB 截断可配置；记录截断指标 |
| 删除重建 bot_id 导致 revision 回退 | 独立 bot 计数器表，不随渠道/应用删除；旧租约无法阻塞新配置 |
| 网关与平台时钟偏差 | 平台按自身时间拒绝过期渠道的 online 上报；网关按快照时间本地定时；部署机启用 NTP |
| 内部状态到公开状态映射遗漏 | 契约测试覆盖 7.2 全部内部状态，未知状态默认不上报 |
| 网关在平台 run 完成后、回复企微前崩溃 | 消息重投时平台返回 204，首次投递不重复回复，用户可能收不到本次回答；答案已保存在会话历史，管理台可见；不重放回答以避免重复消息 |

后续可扩展：

- 语音转写接入。
- 图片/文件理解。
- 模板卡片、欢迎语、主动推送。
- 每应用多个企微机器人。
- 平台侧 Redis 与多 API 进程。
- 渠道级配额和审计。
