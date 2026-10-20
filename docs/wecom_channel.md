# 企业微信智能机器人渠道

企业微信渠道由两部分组成：

- 平台侧（`app/api_server`）：保存机器人配置、管理发布与启停、下发网关快照、接收状态、执行 Agent 并记录脱敏会话。
- 独立网关（`app/channel_gateway`）：与企微保持 WebSocket 长连接，完成消息去重、并发控制和流式回复。进程可以单独部署和升级。

```text
企业微信 --WebSocket--> 渠道网关 --HTTP/NDJSON--> 平台 API --Agent--> 知识库/模型
                          |  ^
                 snapshot |  | status
                          v  |
                       平台 /internal/v1/wecom
```

首版只支持纯文本，支持单聊和群聊。群聊内所有成员共享一个会话（`group:<chatid>`）。

## 前置条件

1. 在企业微信管理后台创建智能机器人，取得 `bot_id` 和 `secret`。
2. 为机器人启用长连接（API 模式）回调，使网关可以主动建立连接；无需配置公网回调 URL。
3. 平台侧和网关使用同一套 >32 字符的内部令牌。

## 平台侧配置

```bash
NIANLUN_INTERNAL_TOKEN=<至少 32 字符，平台与网关共用>
NIANLUN_GATEWAY_CONTROL_URLS=http://127.0.0.1:3100
NIANLUN_GATEWAY_CONTROL_TOKEN=<至少 32 字符，平台调用网关控制接口>
NIANLUN_GATEWAY_CONTROL_TIMEOUT_SECONDS=2
NIANLUN_WECOM_MAX_CONCURRENT=32
NIANLUN_WECOM_RUN_TIMEOUT_SECONDS=120
NIANLUN_WECOM_CONVERSATION_RETENTION_DAYS=30
```

`NIANLUN_INTERNAL_TOKEN` 为空时企业微信渠道不可用，`/internal` 路由返回 503。
`NIANLUN_GATEWAY_CONTROL_URLS` 支持逗号分隔的多个地址，平台会依次尝试。
平台不保存原始 `userid`、`chatid` 或 `msgid`，只保存 `wc_<sha256 前缀>` 会话标识和脱敏后的会话内容。

`/internal/v1/wecom/*` 仅供网关访问，必须通过私有网络或反向代理白名单限制，
不得与 `/v1`、`/api/v1/runtime` 一起暴露到公网。网关控制端口同样只监听私有地址。

## 启动网关

将网关配置填写到项目根目录的 `.env`。网关启动时显式读取该文件，
不依赖当前工作目录；已经设置的进程环境变量优先于 `.env`。

```bash
uv sync --all-groups
uv run python -m app.channel_gateway
```

主要环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NIANLUN_INTERNAL_TOKEN` | 无（必填） | 调用平台 `/internal`，至少 32 字符且无首尾空格 |
| `NIANLUN_GATEWAY_CONTROL_TOKEN` | 无（必填） | 校验平台下发的控制请求，至少 32 字符且无首尾空格 |
| `NIANLUN_API_URL` | `http://127.0.0.1:8000` | 平台内部地址 |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `3100` | 控制监听地址；不要暴露公网 |
| `GATEWAY_LOG_LEVEL` | `INFO` | 日志级别 |
| `GATEWAY_OWNERSHIP_BACKEND` | `local` | `local` 或 `redis` |
| `REDIS_URL` | 无 | `redis` 后端必填 |
| `GATEWAY_REDIS_KEY_PREFIX` | `nianlun:channel-gateway` | Redis key 前缀 |
| `GATEWAY_LEASE_TTL_MS` | `30000` | 所有权租约时长 |
| `GATEWAY_LEASE_SAFETY_MS` | `5000` | 租约安全余量，必须小于 TTL；续租间隔随剩余安全期限缩短 |
| `GATEWAY_SNAPSHOT_INTERVAL_SECONDS` | `30` | 快照轮询间隔 |
| `GATEWAY_STATUS_HEARTBEAT_SECONDS` | `30` | 在线状态心跳间隔 |
| `GATEWAY_CONNECT_TIMEOUT_SECONDS` | `10` | 单次连接超时 |
| `GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS` | `30` | 首事件超时 |
| `GATEWAY_WORKFLOW_TIMEOUT_SECONDS` | `150` | 单次调用总时限，需大于首事件超时 |
| `GATEWAY_MAX_CONCURRENT` | `32` | 网关并发处理消息数 |
| `GATEWAY_MAX_MESSAGE_BYTES` | `65536` | 单条入站文本上限 |
| `GATEWAY_MAX_ANSWER_BYTES` | `1048576` | 单次回答接收上限，超出取消 |
| `GATEWAY_REPLY_MAX_BYTES` | `20480` | 发送给企微的单条回复截断上限 |
| `GATEWAY_STREAM_INTERVAL_MS` | `300` | 流式增量最小发送间隔 |
| `GATEWAY_STREAM_KEEPALIVE_SECONDS` | `15` | 流式保活间隔 |
| `GATEWAY_DEDUP_MAX_ENTRIES` | `10000` | 网关去重条目上限 |
| `GATEWAY_DEDUP_TTL_SECONDS` | `300` | 网关去重窗口 |

健康检查：

- `GET /health`：进程存活。
- `GET /ready`：快照已加载且可接收连接。
- `GET /internal/metrics`：Prometheus 文本指标，需要 Bearer 控制令牌。

### 单实例与多实例

- `GATEWAY_OWNERSHIP_BACKEND=local`：单机模式。同一机器人只能由一个网关进程连接，适合本地开发和单实例部署。
- `GATEWAY_OWNERSHIP_BACKEND=redis`：多实例模式。多个网关共享 Redis 租约，通过 fencing 保证同一时刻只有一个实例连接某个机器人；进程崩溃后租约过期并由其他实例接管。所有实例必须使用相同的 Redis 前缀和令牌。
- 平台通过快照把已启用渠道分配给网关；网关也会周期刷新快照。渠道配置更新、启停或换版本后会自动重连。

## 控制台操作

1. 打开 Agent 的“发布渠道”，创建“企业微信”渠道，填写 `bot_id`、`secret`、默认回复和可选有效期。
   `secret` 只写不读；编辑时留空表示不修改。
2. 在“版本管理”创建不可变版本。
3. 在“发布渠道”选择企业微信并发布。首次部署会自动启用渠道；后续换版本保持当前启停状态。
4. 渠道出现在列表中后，点击“连接企业微信”触发网关立即连接；网关心跳和快照轮询也会自动建立连接。
5. 状态含义：

| 状态 | 含义 |
| --- | --- |
| 在线 | 网关已认证并保持长连接 |
| 离线 | 未连接、已停用或渠道已过期 |
| 异常 | 认证失败、网络错误或连接冲突；悬停可看错误码 |
| 状态未知 | 超过 90 秒没有收到网关状态 |
| 已过期 | 超过配置的有效期；必须更新有效期后才能发布或重新启用 |

停用渠道后快照立即移除该机器人，网关断开连接；重新启用需要再次部署或显式启用。
更新配置会递增配置版本并重置状态，网关按新配置重连。

## 消息与回复

- 单聊会话键为 `single:<userid>`，群聊为 `group:<chatid>`；平台只保存哈希后的 `user_key`。
- 网关按 `msgid` 生成幂等键并重试同一请求，避免重复回答。
- 仅文本消息会进入 Agent；图片、文件等返回固定提示，空消息或超长消息返回统一提示。
- 网关立即发送 `stream_id` 开始流式回复，按配置间隔合并增量，空闲时发送保活。
- 平台超时、取消或回答超过 1 MiB 时，网关结束流并提示稍后重试。
- `default_response` 用于平台不可达、并发超限等无法取得回答的场景。

## 排障

- 控制台状态异常为 `AUTH_FAILED`：检查 `bot_id`、`secret` 是否正确，以及机器人是否已启用长连接。
- `NETWORK_ERROR`：检查网关到企业微信的网络和 DNS。
- `CONNECTION_CONFLICT`：同一机器人在别处建立了连接；确认没有重复部署，或在多实例场景启用 Redis 所有权。
- 网关日志中的企微 errcode 会映射为 `WECOM_*` 代码；846604–846609 分别表示回调过期、回调无效、重复回复、限流、流过期和未订阅。
- 平台侧可用 `GET /internal/v1/wecom/snapshot`（Bearer 内部令牌）确认已下发哪些渠道。
- 网关与平台时间偏差过大会导致渠道有效期和状态判断异常，请保持 NTP 同步。
