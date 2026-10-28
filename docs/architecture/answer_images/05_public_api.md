# 发布 API 与图片授权

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md)

## 职责与依赖

为原生 API 提供可选图片输出、受限公开访问、显式刷新和幂等重放。

依赖 [归档模块](03_storage.md) 的事务和撤销规则，向 [OpenAI 模块](06_openai.md) 提供统一标准 Markdown 投影；公开原生流式也采用该模块的缓冲规则。第一阶段不开启公开图片入口。

## 计划改动位置

以下是设计约定的改动位置。多个模块共用契约和实现，不建立重复流程。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 修改 | `app/api_server/apis/v1/runtime_schemas.py` | include_images、image_transport、公开附件白名单及刷新请求/响应。 |
| 修改 | `app/api_server/apis/v1/runtime/api.py` | 公开图片读取、访问刷新与原生结果响应。 |
| 修改 | `app/api_server/services/application_invocation_service.py` | 图片开关、幂等指纹、历史加载、公开投影与完成事务。 |
| 新增 | `app/api_server/services/answer_image_access_service.py` | 单图 grant 签发/刷新，每次检查 Key、渠道、客户端与来源。 |
| 新增 | `app/api_server/services/answer_image_projection.py` | 规范答案转标准 Markdown，纯图及失败说明共用。 |
| 修改 | `app/api_server/config/settings.py` | 公开 base URL 和有效期；仅 URL 模式在地址配置缺失时提前拒绝图片请求。 |
| 修改 | `.env.example` | 新增配置的安全示例，不包含密钥。 |
| 修改 | `deploy/application-api.nginx.conf` | 精确放行公开图片/刷新路径，查询 token 日志脱敏。 |
| 修改 | `app/api_server/main.py` | 显式关闭 uvicorn 原始 access log，保留不含 query 的结构化请求日志。 |
| 核查并补测试 | `app/api_server/middleware/request_tracking.py` | 沿用仅记录 path 的请求日志，不输出 query、凭据和响应正文。 |
| 修改 | `docs/application_api.md` | 原生调用、过期刷新及部署说明。 |

<a id="s9"></a>

## 发布 API 与公开图片访问

<a id="s9-1"></a>

### 原生 API

`PublicChatRequest` 拟新增 `include_images: bool = false` 和 `image_transport: Literal["url", "data_url"] = "url"`，严格校验。
未开启图片却指定 data_url 时拒绝。默认值、跨字段校验和 OpenAI 对应请求头见 [09](09_transport.md)。
`PublicChatResponse` 拟增加默认空的 `images`，使用专门的公开 DTO 白名单转换，
不直接暴露内部目录、路径和 blob 摘要。
公开图片 DTO 包含附件 `id`、`ref_ids`、`citation_ids`、原文图题、原始尺寸、`orientation`、状态、原因、`url` 和 `expires_at`；
`url` 可按请求承载 HTTP(S) grant URL 或 Data URL；不可用时为 null，内联模式 `expires_at` 恒为 null。
完成结果同步增加 `image_transport`（旧结果默认 url）和最终 `answer_output_mode`（旧结果默认 text）。

`include_images=true` 时，原生响应的 `answer` 是从内部规范 Markdown 原位解析得到的标准 Markdown，
图片目标为本次 grant URL 或服务端从可信原始字节编码的 Data URL，并返回同一份答案附件清单。纯图模式内部答案仍可为空，公开 `answer`
则由所选图片生成有序标准 Markdown。不可用图片转换为转义后的同语言位置说明，不保留未知 scheme。
内部消息保存规范正文；URL 模式的 run 快照保存标准投影且重放不重签链接，
Data URL 模式保存版本化的投影描述、重放时重新授权并确定性编码。详细快照语义见 [09](09_transport.md)。
写入字段来源和同事务要求见 [规范/公开完成结果](03_storage.md#completion-projections)，
不能因当前 repository 从 `response.answer` 写消息而把公开链接写入规范历史。
`include_images=false` 时不开放图片目录、选择工具和标记输出，保持原有文本行为。
图片开关和标准化的 image_transport 进入幂等请求指纹；同幂等键修改任一选项应返回冲突，不再次执行。
API 返回所有在本轮资源预算内被答案明确引用的图片；超限/不可用位置明确说明，
来源面板专用附件不自动授予公开访问，也不自动附到答案末尾。
公开响应、run 的公开结果快照和授权签发均过滤 `display_in_answer=false` 的来源预览附件，
不返回其元数据、私有 URL 或公开 grant；不能直接序列化控制台的完整 `images`。
同一附件同时被答案引用时，按答案图片处理并保留有效来源绑定。
内联模式不需要 public base URL，也不签发 grant；超限位置明确返回 `inline_budget_exceeded`，
不静默降级到可能不可达的 URL，已发送字节无法撤回。控制台的图片输出仍固定私有 URL。

<a id="s9-2"></a>

### 公开访问链接

普通浏览器的图片标签不能替调用方附带应用 Key，因此不使用“把 Bearer Key 塞进 URL”
或“返回需要管理权限的图片地址”方案。

拟提供 `GET /api/v1/runtime/images/{grant_id}?token={opaque_token}`：

- token 为高熵随机的单图只读凭据；授权表只保存摘要，不能用于聊天或访问其他图片。
- 默认有效期 24 小时，且不超过所属 run 的结果保留截止时间；同一 grant 可重复读取以支持浏览器重试，不使用一次性 token。
- 每次读取校验 token、过期时间、附件有效来源、所属会话、客户端和应用渠道状态。
- grant 绑定签发时的 Key ID，并校验该 Key 是否仍有效；轮换期间其他 Key 的 grant 不受影响。
- 不依赖额外的 `user_id` 查询参数完成鉴权；该链接本身就是受限的持有者授权。
- 使用 no-store、no-referrer 和 nosniff；网关与应用日志不得记录 token 查询值。

明确取舍：链接在有效期内可被持有者转发，不提供按终端用户身份校验的永久公开链接。
接入方需要更严格的身份控制时，应通过自己的服务端下载并代理图片。

公开地址由显式配置的后端 public base URL 生成，不能读取不可信 Host 或回退到 localhost。
第二阶段增加该设置时同步 `.env.example`、配置测试与部署文档。

<a id="s9-3"></a>

### 过期、刷新与幂等

首次成功调用把图片清单、grant 及过期时间与运行结果一起保存。只有第二阶段完成且具有
`result_expires_at` 的新 run 才能签发公开图片 grant；升级前 `result_expires_at=NULL` 的旧 run
仍可查询文字结果，但图片授权和刷新明确拒绝，不把 NULL 当成无限期。
URL 模式幂等重放返回同一份结果，不重新调用模型，也不隐式重签图片链接。
URL 模式保存的响应快照会包含带 token 的 URL，这是精确重放的必要数据；因此不能声称整个数据库
只保留 token 摘要。响应快照按现有运行结果限制访问，不进入日志、普通运行列表或测试快照。
grant 验证只查询授权表中的摘要，不依赖从响应快照读取凭据。
普通运行列表、日志、审计输出和提交到版本库的测试快照不得泄漏 grant token，检查范围包括
`result.answer` 内嵌链接与 `images[].url`，不能仅移除结构化 URL 字段。
Data URL 模式不持久化 Base64；仅保存内部投影描述，由统一结果加载器重建公开响应。
正常重放相同，附件撤销后不再返回字节，转换为不可用状态；详见 [09 的幂等规则](09_transport.md)。
通过应用、客户端及原 `user_id` 归属校验的 run 结果详情按原 transport 返回答案和图片投影，
供 SDK 恢复展示及判断是否刷新；无权请求必须拒绝。`expires_at` 是过期时间，不是访问凭据。

链接过期后，接入方通过拟新增的
`POST /api/v1/runtime/apps/{app_id}/runs/{run_id}/images/{attachment_id}/access`
携带有效应用 Key 显式获取新链接。仅允许原 run 的同一客户端访问；不接受“知道附件 ID 即可刷新”。
刷新请求还须提交与原 run 一致的 `user_id`，沿用现有运行结果查询的归属检查，
不能仅凭同客户端身份放宽既有 user_id 边界。该字段仍是接入方声明的审计标识，不代替用户认证。
刷新不修改原 run 结果、文字答案或幂等快照，不重复计算模型用量。
刷新接口仅用于原 `url` 结果；内联结果请求此接口返回 409，不通过刷新绕过 transport 指纹。
旧 grant 不延长；新 grant 具有新的过期时间。附件删除、渠道停用或运行记录不再可访问时拒绝刷新。
续聊 checkpoint 过期不提前影响保留中的结果；run 结果到期后，详情、重放与刷新统一返回 410，
不得因快照已清理重新执行同一幂等键。结果保留默认 30 天，旧 run 不自动过期；
时间边界、清理责任及清理后 404/410 的区别见 [生命周期矩阵](03_storage.md#retention-lifecycle)。

<a id="s9-4"></a>

### 网关

`deploy/application-api.nginx.conf` 必须精确加入公开图片读取与访问刷新路径，
继续拒绝控制台、知识库、模型和任意文件目录。公开图片端点遵守现有 API 启用总开关。
缺少公开地址配置时，URL 图片请求在受理前明确失败；Data URL 与默认文本调用不受影响。

新增 grant、附件 ID 沿用当前 app/run 的 UUID 格式，示例中的 `app_01`、`grant_01`、
`attachment_01` 仅为易读占位，不是实际 ID 语法。网关分别增加以下两条有界 UUID 路径：

- `GET /api/v1/runtime/images/{grant_id}`。
- `POST /api/v1/runtime/apps/{app_id}/runs/{run_id}/images/{attachment_id}/access`。

不为演示 ID 放宽所有现有路由正则；使用实际生成的 UUID 测试允许路径、方法、尾部多余段及管理路径拒绝。

<a id="access-logging"></a>

### 应用进程访问日志

公开图片上线前，`main.py` 的 `uvicorn.run` 显式设置 `access_log=False`。
直接用 uvicorn CLI 启动须传 `--no-access-log`；gunicorn/多 worker 等部署同样关闭包含原始 query
的访问日志，并在部署文档列出启动参数。继续使用现有 `RequestTrackingMiddleware` 的结构化日志，
保留 request ID、method、`scope.path`、状态码与耗时；当前该中间件已不记录 query，无需另造请求日志。

这是第二阶段公开图片的上线门槛。nginx、ASGI server、stdout/容器采集及错误日志均纳入检查，
不能只关 nginx 日志，也不能认为应用 middleware 能消除 uvicorn 已输出的日志。
如将来需要恢复 server access log，必须先提供经真实 formatter 验证的 query 脱敏方案。
验收用 fake token 经实际 uvicorn 启动入口请求并捕获输出，验证 token 不出现且结构化请求指标仍在；
仅使用 TestClient 无法覆盖 uvicorn access logger。

## 模块验收

扩展 tests/api_server/test_public_runtime.py、tests/api_server/test_application_runtime.py 和发布测试；覆盖默认文本兼容、归属、过期、撤销、刷新、幂等与网关白名单。

具体边界场景见 [完整验收清单](08_validation.md)。
