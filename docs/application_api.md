# 发布 Agent 并通过 API 调用

## 部署边界

使用一个后端进程、一个 worker。后端监听 `127.0.0.1:8000`，或限制在私有网络。
控制台及 `/api/v1/apps`、模型、知识库、上传、`/docs`、OpenAPI 必须通过私有入口访问。
`/internal/*`（网关快照、状态和运行接口）只允许独立渠道网关从私有网络访问，不得对外暴露。
不要直接将后端端口映射到公网。应用 Key 不具有管理员权限，也不能保护已有的管理接口。

`deploy/application-api.nginx.conf` 提供只允许公开调用路径的网关配置；将其包含在 nginx 的 http 配置内，
在可信负载均衡器终止 HTTPS，或配置本站 TLS。向公网开放之前验证后端端口不可直连、旧 chat 和管理接口均返回 404。
首版不支持多 worker、多副本和 SSE 断线续传。限制和 checkpoint 数据保存在本地，数据目录应持久化。

设置 `NIANLUN_API_APPLICATION_ENABLED=true` 启用调用，其他限额见 `.env.example`。
不设置时公开入口返回 404，控制台仍可准备应用与密钥。
原有部署启动命令继续可用；不要额外启动第二个共享相同数据库/工作区的 API 进程。

## 控制台

进入 Agent 管理，确认 Agent 配置中的知识库和模型后，打开“发布渠道”，点击“创建发布渠道”，
选择“API 服务”或“企业微信”并创建。渠道列表只显示已创建的渠道；个人微信暂未支持。
每个 Agent 可同时创建一个 API 渠道和一个企业微信渠道，多个 API 接入系统通过不同客户端及 Key 区分。
企业微信需要单独运行长连接网关，配置与启动方式见 `docs/wecom_channel.md`。
创建渠道只保存接入入口，不生成发布版本或启用调用。发布首个版本后启用 API，再创建接入客户端及 Key。
已停用渠道重新发布仍保持停用，需要显式恢复。

管理接口 `POST /api/v1/apps/{app_id}/channels` 接收 `{"type":"api"}` 或
`{"type":"wecom","bot_id":...,"secret":...,"default_response":...}`，返回现有 JSON envelope 中的发布状态。
`api_channel_created` 表示 API 渠道是否已创建，`wecom_channel` 表示企业微信渠道状态；重复创建返回 409，
未支持的渠道类型返回 422。企业微信 `secret` 只写不读，响应中仅返回 `has_secret`。
创建状态复用持久化的 API deployment，已有 API 发布自动视为已创建，旧发布接口保持兼容。
Key 在浏览器本地生成，服务端只接收写入并保存摘要；注册成功后立即保存在接入系统的秘密存储中，关闭窗口后不能找回。
不要把 Key 放在网页前端、URL、代码仓库或日志中。轮换时先添加同客户端的新 Key，验证接入后撤销旧 Key。

详情页将“Agent 配置”“版本管理”“发布渠道”“会话”“运行日志”分开。保存草稿不会修改线上版本；
新建 Agent 已保存初始草稿，无需重复保存；修改配置后才启用保存操作。
“版本管理”顶部始终显示当前草稿及保存状态，下方单独列出已创建的版本。
在“版本管理”创建带说明的不可变版本，查看配置差异，也可确认后将历史配置复制回草稿。
点击“发布”选择最新已保存草稿或任意可用历史版本，确认目标渠道与变更后上线。
从草稿发布分为创建版本和渠道部署；部署失败保留已创建版本，可直接重试。
未保存草稿不影响历史版本发布。发布记录保留版本切换与启停操作，回滚通过再次发布历史版本完成。

私有管理接口均使用现有 JSON envelope：

| 接口（前缀 `/api/v1/apps/{app_id}`） | 行为 |
| --- | --- |
| `POST /releases` | `expected_config_version`、可选 `description`（最多 500 字）；仅创建版本 |
| `GET /releases/{release_id}` | 获取指定版本配置 |
| `POST /channels` | `type: "api"`，或企业微信 `bot_id`、`secret`、`default_response`、可选 `expires_at` |
| `GET/PUT /channels/wecom` | 获取或更新企业微信配置；更新需 `expected_config_revision`，`secret` 可省略 |
| `PATCH /channels/wecom` | `enabled`、`expected_deployment_version`；启用或停用企业微信渠道 |
| `POST /channels/wecom/connect` | 请求网关立即连接企业微信 |
| `POST /deployments` | `channel_type: "api" \| "wecom"`、`release_id`、`expected_deployment_version`；部署指定版本 |
| `GET /deployments?limit=20&offset=0&channel_type=api` | 按渠道分页查看部署、启用、停用记录 |
| `GET /conversations?channel=console\|api\|wecom` | 按渠道列出会话；企业微信会话只读且只含脱敏标识 |
| `POST /releases/{release_id}/restore` | `expected_config_version`；复制模型、知识库与运行配置到草稿，保留名称和描述 |

部署要求渠道已创建，且目标版本与当前程序、模型配置和知识库兼容；冲突返回 409，不覆盖他人更新。
原 `POST /publish` 兼容保留。升级前的发布保留原状态，历史记录只记录升级后的操作。
新会话使用当前渠道版本，已有会话继续原发布版本。
每轮调用自动使用最新已提交知识库，记录实际内容版本；处理中或失败的新文件不会作为完整内容读取。
停用会阻止后续调用和续聊；已受理调用可以正常结束。
仍有有效发布/会话引用的模型不能更改 endpoint 或模型名称，复制 Profile 后重新发布即可切换。

## OpenAI 兼容调用（推荐）

API 服务提供 `POST /v1/chat/completions`、`GET /v1/models` 和 `GET /v1/models/{model}`。
控制台展示的 Base URL 为服务地址加 `/v1`；`model` 使用 Agent ID，不随名称变更而改变。
对外部署时应通过 `VITE_PUBLIC_API_BASE` 或运行时 `window.NIANLUN_PUBLIC_API_BASE` 把该地址指向公开网关；
未配置时控制台会显示本机控制台地址并提示替换，请勿把内网地址交给调用方。
每个客户端 Key 仅能访问所属 Agent，模型列表只返回该 Key 有权访问的已发布 Agent。
已有发布与 Key 可继续使用，无需为兼容入口创建额外渠道。

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=f"{os.environ['NIANLUN_BASE'].rstrip('/')}/v1",
    api_key=os.environ["NIANLUN_APP_KEY"],
)
response = client.chat.completions.create(
    model=os.environ["NIANLUN_APP_ID"],
    messages=[{"role": "user", "content": "请概括报销要求"}],
)
print(response.choices[0].message.content)
```

```bash
curl "$NIANLUN_BASE/v1/chat/completions" \
  -H "Authorization: Bearer $NIANLUN_APP_KEY" \
  -H 'Content-Type: application/json' \
  --data "{\"model\":\"$NIANLUN_APP_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"请概括报销要求\"}],\"stream\":false}"
```

成功响应使用标准 `chat.completion` 格式，直接包含 `id`、`created`、`model`、`choices` 和 `usage`，
不套用管理接口的 JSON envelope。usage 汇总本轮检索及回答调用，使用 `prompt_tokens`、`completion_tokens`、
`total_tokens`；上游未提供用量时为 null，不伪造零值。运行日志保留发布版本、知识库实际内容版本和内部引用证据。

多轮对话由调用方每次提交完整 `messages`。每次调用创建独立执行上下文，使用当前发布版本和最新已提交知识库，
不与之前调用的服务端历史叠加。支持 user、assistant、system、developer 文本角色；developer 在运行时映射为 system。
消息正文支持字符串和仅含 `type: "text"` 的内容块，最后一条必须是非空 user 消息。
每条最多 32000 字符、总计最多 128 条消息和 128000 字符，同时受 Agent 模型上下文预算约束。
兼容入口不自动摘要或截断调用方历史；完整历史及执行上下文超出模型容量时明确失败。
可选 `user` 用于接入方审计标识，不作为身份认证；不传时使用固定审计标识。

设置 `stream=True` 后，SDK 按 `chunk.choices[0].delta.content` 读取增量。
SSE 使用 `data: {...}` 的 `chat.completion.chunk`，成功时依次输出结束 chunk 和 `data: [DONE]`。
`stream_options={"include_usage": True}` 会在 `[DONE]` 前增加 choices 为空的用量 chunk。
v1 在独立回答节点生成时实时输出；v0 需要确认最后一个模型轮次，确认后输出最终回答，避免泄漏候选内容和工具过程。
流中失败输出 `{"error":{...}}`，不发送成功结束标记；客户端断开或超时会取消执行，并在 worker 退出前保留并发占用。

`Idempotency-Key` 为可选请求头，需防止重复执行时为每条逻辑调用生成唯一值，网络重试复用原值和原请求。
SDK 可通过 `extra_headers={"Idempotency-Key": message_key}` 传入；不提供时每次 HTTP 请求都视为新调用。
已完成流式重试重放一次完整答案。正在执行的重试返回 409 和 Retry-After，失败调用不自动重新执行。

第一版只支持文本、单回答（`n=1`）。`temperature`、`max_tokens`、外部 tools、图片、音频、
`response_format` 等未支持参数返回 HTTP 400，避免覆盖已发布的 Agent 设置或静默忽略调用方意图。
Responses API 尚未提供。兼容入口错误使用 `{"error":{"message":...,"type":...,"param":...,"code":...}}`，
保留 HTTP 状态、Retry-After、Location 和 X-Request-Id；现有管理与原生调用的错误格式不变。

## 原生调用（保留兼容）

应用、Key 和用户 ID 应由你的服务端配置提供。每条逻辑消息生成一个幂等键；网络重试复用该键及原请求。
`user_id` 是接入系统内的用户标识，接入系统负责认证用户，不能让不可信终端任意冒充其他用户。

```bash
curl "$NIANLUN_BASE/api/v1/runtime/apps/$NIANLUN_APP_ID/chat" \
  -H "Authorization: Bearer $NIANLUN_APP_KEY" \
  -H "Idempotency-Key: $NIANLUN_MESSAGE_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"message":"请概括报销要求","user_id":"employee-123","response_mode":"blocking"}'
```

成功返回 `{code:200,message:"success",data:{...}}`，data 包含 answer、conversation_id、run_id、release_id、
引用证据、usage 和 knowledge_base_content_version。后续消息传入服务端返回的 conversation_id；未知 ID 返回 404。

```python
import os
import uuid
import httpx

response = httpx.post(
    f"{os.environ['NIANLUN_BASE']}/api/v1/runtime/apps/{os.environ['NIANLUN_APP_ID']}/chat",
    headers={
        "Authorization": f"Bearer {os.environ['NIANLUN_APP_KEY']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={
        "message": "请概括报销要求",
        "user_id": "employee-123",
        "response_mode": "blocking",
    },
    timeout=330,
)
response.raise_for_status()
result = response.json()["data"]
print(result["answer"])
```

## 流式与重试

将 response_mode 改为 streaming，消费 SSE `ready -> message -> done`。
首版 message 输出一次完整最终答案，执行期间有注释心跳；候选回答与内部 trace 不公开。
只有 done 表示成功持久化。执行失败以 error 结束；鉴权或参数错误发生在流开始前，使用正常 JSON HTTP 错误。

受理响应的 Location 是调用结果查询地址，需要同客户端有效 Key。也可以使用
`GET /api/v1/runtime/apps/{app_id}/runs/{run_id}?user_id={user_id}`。
同幂等键完成重试返回原结果；正在执行返回 409 和 Retry-After，不会再调用一次模型。
失败、超时或取消不自动重跑；需要再次尝试时创建新会话并使用新消息幂等键。
已完成会话支持重启后续聊；中断上下文拒绝续聊，避免把半轮执行当成完整历史。

默认限额为全局 8、每应用 4、每客户端 2 个并发，每客户端每分钟 60 次，总时限 300 秒。
429 表示限额不足，504 表示超时，502/503 表示上游或运行依赖故障；使用 X-Request-Id 和 run_id 定位。
400 表示请求不受支持，或完整 messages 超出模型上下文预算（受理时判定超出请求上限，或运行后判定超出模型预算）；
两者都以 `invalid_request_error` 返回，不会退化为 500/502。
超时后上游请求可能仍在结束中，实际 worker 退出前不会释放并发额度。
会话默认空闲有效期 30 天；过期 checkpoint 会清理，消息及调用审计不因过期自动删除。
OpenAI 兼容入口由调用方自带完整 messages、不建会话，属于无状态调用：每次调用结束后立即回收其 checkpoint thread，
因此重复调用不会累积 checkpoint，也不能用于续聊；续聊请使用公开 `/chat` 并携带 `conversation_id`。
首版调用记录和幂等键保留，至少覆盖 30 天重试窗口；额外历史保留会占用磁盘，需通过后续显式管理策略清理。
