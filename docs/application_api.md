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
从仓库根目录使用以下任一启动方式；不要额外启动第二个共享相同数据库/工作区的 API 进程：

```bash
uv run python -m app.api_server.main
# 或由 uvicorn CLI 加载同一应用；必须显式关闭原始 access log。
uv run uvicorn app.api_server.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

模块入口已设置 `access_log=False`。保留应用的 `request.started/request.completed` 日志，
其中只有 request ID、method、path、状态码和耗时，不含查询串、Authorization 或正文。
公开图片 URL 的 query token 是单图访问凭据，不得记录到代理、ASGI、stdout/容器采集或错误日志。
示例 nginx 在 http/server 两级将原始 error log 写入 `/dev/null`，防止上游失败或无效请求的
错误消息带出完整请求行；它同时关闭 access log。采用专用网关配置，不再覆盖这些设置或在
负载均衡器、WAF、APM 中采集原始 URL；保留错误诊断时须先验证能清除 query 的真实格式化链路。

网关按方法与精确路径放行下表，其余路径、非 UUID 标识、额外段、尾斜线和错误方法返回 404。
表中每条路径另允许 OPTIONS 转发给应用 CORS 校验；CORS 仍只允许配置的 Origin，
允许鉴权、幂等键和图片请求头，并暴露 Location 与 X-Request-Id。

| 方法 | 公开路径（标识均为标准带连字符 UUID） |
| --- | --- |
| POST | `/api/v1/runtime/apps/{app_id}/chat` |
| GET | `/api/v1/runtime/apps/{app_id}/runs/{run_id}` |
| GET | `/api/v1/runtime/images/{grant_id}` |
| POST | `/api/v1/runtime/apps/{app_id}/runs/{run_id}/images/{attachment_id}/access` |
| POST | `/v1/chat/completions` |
| GET | `/v1/models`、`/v1/models/{app_id}` |

部署时在实际 nginx 上执行配置检查，并验证上述允许/拒绝矩阵、带 fake token 的成功与失败请求日志；
配置路由单元测试不能代替 nginx 的真实转发、错误日志和 TLS 验收。

### 图片目录升级与离线维护

首次写入 snapshot v3 前，先停止共享存储的全部旧 API、worker 和清理进程，
释放读引用并备份一致的 SQLite 与工作区，再部署支持 v2/v3 的同版本代码。
维护锁不能识别未接入协议的旧进程，部署人员必须确认其退出。写入 v3 后只能回退到
支持 v3 的程序；恢复纯 v2 程序需要另行授权恢复整套一致备份，不能单独回退指针。

图片补建使用 `python -m app.api_server.backfill_document_images --knowledge-base-id KB_ID`；
可加 `--document-id DOCUMENT_ID`、`--batch-size 1..100`（默认 100）和 `--dry-run`。
先正常停止 API 和导入任务，等待所有 worker 完成关闭及 SQLite checkpoint。
dry-run 若发现非空 WAL 会明确拒绝，不忽略 WAL、不自动 checkpoint 或改写数据库。
真实 MinIO/nginx、旧库升级与整套恢复的隔离验收命令见
[`tests/acceptance/README.md`](../tests/acceptance/README.md)。默认测试不启动这些基础设施；
已显式启用的验收会自行创建临时服务和 bucket，不使用生产 endpoint 或凭据。
dry-run 的候选、预算和预计 revision 是预测，实际提交数单独报告。

API 与 `import_datasets` 在迁移、恢复和存储初始化前持有共享生命周期维护锁；
补建（含 dry-run）在扫描前非阻塞取得独占锁。冲突立即退出。锁文件位于
`NIANLUN_API_DATA_DIR/.storage-maintenance.lock`，部署和清理不得删除或替换它。
所有使用同一数据库/工作区的入口必须使用相同的解析后 data_dir 配置。
锁顺序固定为维护锁、workspace 锁、数据库事务。

通用 tree/FTS/vector/Agent/knowledgebase CLI 不直接操作 API 工作区；检测到
`snapshots/`、`.api-server.lock`、`.tree-build-options.json` 或 ownership 标记时
会在业务读取、清理或外部构建前拒绝对应路径及其后代。索引维护走 API，
图片补建走上述离线命令。不要移除这些标记或把 API 路径伪装为旧版工作区。

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

输入仅支持文本，单回答（`n=1`）；输出默认文本，可按下节显式启用原图 Markdown。
`temperature`、`max_tokens`、外部 tools、图片/音频输入、
`response_format` 等未支持参数返回 HTTP 400，避免覆盖已发布的 Agent 设置或静默忽略调用方意图。
Responses API 尚未提供。兼容入口错误使用 `{"error":{"message":...,"type":...,"param":...,"code":...}}`，
保留 HTTP 状态、Retry-After、Location 和 X-Request-Id；现有管理与原生调用的错误格式不变。

### 可选图片输出与结果恢复

图片输出要求 v5 发布、服务端 `NIANLUN_API_ANSWER_IMAGES_ENABLED=true` 与本次请求 opt-in。
v4 发布请求图片受理前返回 409；WeCom 仍只支持文字。该功能不向模型提供图片像素，也不启用视觉理解。

| 请求入口 | 开启图片 | 选择传输 |
| --- | --- | --- |
| 原生 JSON | `include_images: true` | `image_transport: "url"`（默认）或 `"data_url"` |
| OpenAI 请求头 | `X-Nianlun-Include-Images: true` | `X-Nianlun-Image-Transport: url`（默认）或 `data_url` |

未开启图片配 `data_url` 明确拒绝；原生返回 422，OpenAI 返回 400。图片开关和 transport
纳入幂等指纹，省略 transport 等价于 url，同幂等键修改这些选项返回冲突。
两种输出均保留原位文字与图片顺序，OpenAI `content` 仍为标准 Markdown 字符串；
原生 result 同时返回 `images`、`image_transport`、`answer_output_mode`。
纯图请求也返回有序 Markdown；不可用位置保留说明，不把来源预览附件自动公开。

URL 模式须设置 `NIANLUN_API_APPLICATION_PUBLIC_BASE_URL` 为可信公开 API 根地址，不能从请求 Host 推导。
图片使用单图 grant；默认 `NIANLUN_API_ANSWER_IMAGE_GRANT_HOURS=24`，且不超过运行结果期限。
每次读取仍检查 Key、渠道、来源和有效期。标准响应带 no-store，浏览器图片读取不需要再附应用 Key。
URL 是持有者凭据，接入方不应写日志或放入分析埋点。

Data URL 模式不要求公网图片地址，不签 grant。可信 PNG/JPEG/WebP 字节直接内联在 Markdown 和
`images[].url`，`expires_at=null`，不提供链接刷新。默认原始单图 1 MiB、本轮 2 MiB、完整原生
JSON envelope 8 MiB，配置分别为 `NIANLUN_API_ANSWER_IMAGE_INLINE_MAX_BYTES`、
`NIANLUN_API_ANSWER_IMAGE_INLINE_TOTAL_BYTES`、`NIANLUN_API_ANSWER_IMAGE_INLINE_RESPONSE_MAX_BYTES`。
图片字节超限保留 `inline_budget_exceeded` 说明，不静默回退 URL；完整响应超限则失败。
Markdown 渲染器和 CSP 可能拒绝 data scheme，调用方须验证兼容，不能为此放开任意 HTML/SVG。
已交付的字节无法撤回；后续详情读取/重放会重新校验权限，来源失效时不再输出原图。
服务端不把 Base64 写入持久化结果；调用方回传的 assistant 图片历史只作指代线索，不抓取或解码。

阻塞、流式及幂等重放均返回同一 `Location` 查询地址；无需从 completion ID 推导 run ID。
Location 相对于所调用的公开 API 根地址解析，携带原 user_id；OpenAI 未传 user 时使用 `openai`。
浏览器可通过已暴露的 Location 头恢复结果。SDK 应校验同源和精确 run 路径后才发送应用 Key，
禁用自动跨域重定向，不向图片 URL 转发 Key。以下示例使用 OpenAI SDK 的原始响应和 httpx，
只在 URL 即将过期时刷新；刷新结果仅更新接入方展示状态，不改变保存的原始完成结果：

```python
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from openai import OpenAI

api_root = os.environ["NIANLUN_BASE"].rstrip("/")
app_id = str(uuid.UUID(os.environ["NIANLUN_APP_ID"]))
app_key = os.environ["NIANLUN_APP_KEY"]
user_id = "employee-123"
expected = urlsplit(api_root)
if (
    expected.scheme not in {"http", "https"}
    or not expected.hostname
    or expected.username is not None
    or expected.password is not None
    or expected.query
    or expected.fragment
):
    raise ValueError("Invalid configured API root")

sdk = OpenAI(base_url=f"{api_root}/v1", api_key=app_key)
raw = sdk.chat.completions.with_raw_response.create(
    model=app_id,
    user=user_id,
    messages=[{"role": "user", "content": "请介绍系统架构，并展示原图"}],
    extra_headers={"X-Nianlun-Include-Images": "true"},
)
completion = raw.parse()
# Render completion.choices[0].message.content in local display state; do not log it.

run_url = urljoin(f"{api_root}/", raw.headers["Location"])
actual = urlsplit(run_url)
prefix = expected.path.rstrip("/") + f"/api/v1/runtime/apps/{app_id}/runs/"
run_id = actual.path.removeprefix(prefix)
if (
    (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc)
    or actual.username is not None
    or actual.password is not None
    or actual.fragment
    or not actual.path.startswith(prefix)
    or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", run_id)
    or parse_qs(actual.query) != {"user_id": [user_id]}
):
    raise ValueError("Unexpected run result Location")

with httpx.Client(
    headers={"Authorization": f"Bearer {app_key}"},
    follow_redirects=False,
    timeout=30,
) as http:
    response = http.get(run_url)
    response.raise_for_status()
    run = response.json()["data"]
    if run["run_id"] != run_id or run["app_id"] != app_id:
        raise ValueError("Unexpected run identity")
    result = run["result"]
    if result is not None and result["image_transport"] == "url":
        for image in result["images"]:
            if image["status"] != "available" or not image["expires_at"]:
                continue
            expires = datetime.fromisoformat(image["expires_at"])
            if expires > datetime.now(timezone.utc) + timedelta(minutes=5):
                continue
            attachment_id = str(uuid.UUID(image["id"]))
            refresh_url = (
                f"{api_root}/api/v1/runtime/apps/{app_id}/runs/{run_id}"
                f"/images/{attachment_id}/access"
            )
            response = http.post(refresh_url, json={"user_id": user_id})
            response.raise_for_status()
            refreshed = response.json()["data"]
            # Update local display state with refreshed["url"]/refreshed["expires_at"].
```

内联调用在 extra_headers 加 `X-Nianlun-Image-Transport: data_url`，不调用刷新接口。
流式 SDK 使用 `with_streaming_response.create(stream=True, ...)`，在响应上下文内读取 Location 和 SSE。
图片开启后只提前发送安全文字前缀，首图及后续内容等待完成事务后输出；心跳继续，失败不发送成功结束标记。

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
message 输出已确认可公开的文字增量；图片开启时首图及后续内容在持久化成功后按原顺序输出。
执行期间有注释心跳；候选回答与内部 trace 不公开。
只有 done 表示成功持久化。执行失败以 error 结束；鉴权或参数错误发生在流开始前，使用正常 JSON HTTP 错误。

受理响应的 Location 是调用结果查询地址，需要同客户端有效 Key。也可以使用
`GET /api/v1/runtime/apps/{app_id}/runs/{run_id}?user_id={user_id}`。
同幂等键完成重试返回原结果；正在执行返回 409 和 Retry-After，不会再调用一次模型。
URL 模式重放保留首次保存的 URL/到期时间，不隐式续签；Data URL 模式从固定描述重新授权、重建，
资源有效时字节相同，来源撤销后只返回不可用说明，不为精确重放再次披露原图。
刷新只更新接入方持有的展示链接，不修改原始幂等结果或重新执行模型。
失败、超时或取消不自动重跑；需要再次尝试时创建新会话并使用新消息幂等键。
已完成会话支持重启后续聊；中断上下文拒绝续聊，避免把半轮执行当成完整历史。

默认限额为全局 8、每应用 4、每客户端 2 个并发，每客户端每分钟 60 次，总时限 300 秒。
429 表示限额不足，504 表示超时，502/503 表示上游或运行依赖故障；使用 X-Request-Id 和 run_id 定位。
400 表示请求不受支持，或完整 messages 超出模型上下文预算（受理时判定超出请求上限，或运行后判定超出模型预算）；
两者都以 `invalid_request_error` 返回，不会退化为 500/502。
超时后上游请求可能仍在结束中，实际 worker 退出前不会释放并发额度。
会话默认空闲有效期 30 天；过期 checkpoint 会清理，消息及调用审计不因过期自动删除。
运行结果另有 `NIANLUN_API_APPLICATION_RESULT_RETENTION_DAYS`（默认 30 天）截止时间，
到期查询、重放和图片刷新返回 410，不续期或自动重跑；grant 到期可在结果有效且权限仍有效时显式刷新。
旧 run 的空结果期限不等于无限图片授权：旧文字读取保持兼容，图片签发/刷新明确拒绝。
OpenAI 兼容入口由调用方自带完整 messages、不建会话，属于无状态调用：每次调用结束后立即回收其 checkpoint thread，
因此重复调用不会累积 checkpoint，也不能用于续聊；续聊请使用公开 `/chat` 并携带 `conversation_id`。
首版调用记录和幂等键保留，至少覆盖 30 天重试窗口；额外历史保留会占用磁盘，需通过后续显式管理策略清理。
