# 图片传输：URL 与 Data URL

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md)

## 职责与边界

沿用 01–03 的原始二进制归档、可信附件和来源校验，只增加公开响应的传输选择。
客户端只访问 Nianlun 图片接口即可使用 URL，不需要访问底层存储；使用 Data URL 时，
图片字节已在响应内，不需要另一次图片请求。两者均不让模型生成真实图片地址。

| 场景 | 默认与可选方式 |
| --- | --- |
| 控制台 | 固定私有 URL，保留懒加载和受控附件渲染 |
| 原生公开 API | 默认 `url`，可显式选择 `data_url` |
| OpenAI 兼容输出 | 默认 `url`，可显式选择 `data_url`；content 仍为 Markdown 字符串 |
| WeCom | 本阶段仍禁用图片 |
| 将来的视觉模型输入 | 服务端独立决定模型输入编码；不由输出 transport 开关触发 |

Data URL 是 `data:image/png;base64,...` 这样的内联地址。归档仍是 PNG/JPEG/WebP 原始字节，
不会转成 Base64 文件，不做转码或压缩；图片展示仍不代表已经调用视觉模型。

## 计划改动位置

以下列出设计约定的改动位置，与其他模块共用文件和类型，不建立第二套附件系统。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 修改 | `app/api_server/apis/v1/runtime_schemas.py` | 请求/响应 transport 枚举及严格跨字段校验 |
| 修改 | `app/api_server/apis/v1/runtime/openai.py` | 可选传输请求头、受限内联 content、流式分块 |
| 修改 | `app/api_server/services/application_invocation_service.py` | 指纹、准入、投影及结果详情/重放统一加载入口 |
| 新增 | `app/api_server/services/answer_image_projection.py` | URL/Data URL 投影、准确尺寸计费、冻结的渲染计划 |
| 修改 | `app/api_server/repositories/application_runs.py` | 持久化有版本的内部结果描述，禁止把 Data URL 响应直接存入 result_json |
| 修改 | `app/api_server/config/settings.py`、`.env.example` | 内联资源预算及配置边界 |
| 修改 | `docs/application_api.md` | 两种模式、渲染器兼容、数据撤销与网络条件 |

## 请求与响应契约

原生 `PublicChatRequest` 新增 `image_transport: Literal["url", "data_url"] = "url"`，
与既有拟新增 `include_images=false` 配合。

原生调用的请求增量示例（其余字段沿用现有契约）：

```json
{
  "include_images": true,
  "image_transport": "data_url"
}
```

OpenAI 对应请求头：

```text
X-Nianlun-Include-Images: true
X-Nianlun-Image-Transport: data_url
```

不认识的枚举值拒绝；`include_images=false` 配 `data_url` 拒绝，不偷偷启用图片。
原生沿用请求校验 422，OpenAI 使用现有错误格式的 400。两个选项经标准化后进入幂等 fingerprint，
省略 transport 与显式 `url` 等价；相同幂等键改 transport 返回冲突。

原生响应新增 `image_transport`，旧结果默认 `url`。附件继续使用 `url` 字段承载实际展示目标，
不再增加另一份 Base64 字段；其含义由响应 transport 明确：

| 字段 | `url` 模式 | `data_url` 模式 |
| --- | --- | --- |
| `images[].url` | grant HTTP(S) 地址 | 经验证字节的 Data URL |
| `images[].expires_at` | grant 截止时间 | null，已经发出的字节不具有服务器可执行的过期时间 |
| `answer` 图片目标 | 同一 grant 地址 | 同一 Data URL |
| 不可用附件 | url/expires_at 为 null，并有明确状态/原因 | 同左，不以 HTTP URL 静默替代 |

纯图结果生成有序 Markdown；重复图首次嵌图，后续位置使用“见前图”的文字及来源编号，
不重复内联 payload。原生 answer 和 images 清单各出现一次 Data URL 的开销仍须完整计费。
只允许 `display_in_answer=true` 的答案附件进入公开投影，来源预览不内联。

Base64 使用 RFC 4648 标准字母表、正确填充、无换行；前缀 MIME 来自解码校验，限定
`image/png`、`image/jpeg`、`image/webp`。不使用客户端/模型提供的字符串作为可信地址。

## 准入与大小限制

`url` 图片调用在受理前要求 public base URL；`data_url` 调用不要求公网图片地址，
仍要求图片输出启用、v5 发布、正常应用 Key/渠道及新 run 的结果期限。Location 可继续使用相对 run 查询路径。
NULL 期限的旧 run 不补建或内联图片，旧文字结果读取不变。

预算在受理时冻结，URL 模式仍用 07 的原始资源限额；内联模式额外采用：

| 预算 | 默认 | 判定 |
| --- | --- | --- |
| `answer_image_inline_max_bytes` | 单图 1 MiB 原始字节 | 归档前/编码前检查 |
| `answer_image_inline_total_bytes` | 本轮 2 MiB 唯一原始图片字节 | 按首次答案引用顺序分配 |
| `answer_image_inline_response_max_bytes` | 8 MiB | 完整公开原生 JSON envelope 的 UTF-8 大小；含 answer、images、转义及元数据 |

三项均为可配置正整数；不得突破部署已有更小的响应限制。编码长度按 `4 * ceil(N/3)` 加 MIME 前缀
准确计算，不能只用约 33% 的估计。即使 OpenAI 首次只返回 content，准入也按其原生 run 详情的完整投影预算，
避免调用成功却无法查询结果。预算判断不依赖 gzip 压缩率。

单图或本轮图片字节超限：该位置转为 `unavailable/inline_budget_exceeded`，保留顺序、来源和说明，
不签 grant、不退回 URL、不截断 Base64、不只取前几张假装完整。允许后续较小图片在剩余预算内继续加入。
内联前确定该结果，避免无必要地为超限图片归档字节。纯图全部失败按 03 转成文字说明。
即便图片额度未超，完整 envelope 若仍超限则按现有大小错误语义失败，不标记 run completed；
流式已开始时发协议错误且不成功结束。无需新的模型调用补写说明。

## 完成事务与幂等结果

原始 blob、附件及规范消息保持 03 的事务和 [10 后端协调规则](10_blob_storage.md)。编码在 service/worker 中进行，
不得在 async route 阻塞事件循环，也不在数据库写事务里执行长时间解码或网络传输。
服务端先在本地媒体锁或 S3 有效读取租约保护下读取、验摘要并完成有界编码/预算计算，再于完成写事务复查来源、渠道/Key、
结果期限和附件绑定。权限冲突遵守失败语义，不因已编码就返回字节；提交成功后才发出图片内容。
不在客户端传输期间持锁；读取重放同样执行授权/协调检查。S3 远端 GET 在数据库事务外执行，
依 10 的租约保持对象不被 GC，读完且对外输出前复查授权。

`run.result_json` 不能同时被当成任意内部描述和 `PublicChatResponse` 直接解析。增加内部严格类型：

- 旧结果和 `url` 结果：沿用已有公开结果快照，精确保存 URL，无 transport 字段视为 url。
- `data_url` 结果：使用 `storage_format="inline_projection_v1"` 的内部描述，保存公共结果的其他字段、
  图片占位节点/位置、答案附件顺序与元数据、固定 MIME/摘要绑定、固定错误说明及序列化规则版本。
  不保存 Base64、grant 或可被外部调用方直接序列化的私有路径。单纯附件 ID 列表不足以重建有序答案。

该描述与规范消息、附件和 run 完成同事务保存。公开 API 完成返回、run 详情、幂等重放及 OpenAI
统一经 service 的结果加载器转换为 `PublicChatResponse`，不向客户端透出 storage_format。
当前 `public_run`/重放路径直接调用 `PublicChatResponse.model_validate_json` 的位置同步改造并覆盖旧结果读取；
普通列表 `include_result=false` 时不加载/编码 payload，也不把内部描述暴露在列表。
无需新增 Base64 数据库列或改写已有结果；带版本的内部 JSON 与两种快照形态都须严格校验，未知版本明确失败。

首次与重放在来源状态不变、blob 完整且结果有效时，由固定模板和同一字节生成相同 answer/images；
冻结完成时的预算结果，不因后续提高限额让原本不可用的图突然出现。重放仍执行应用/客户端/user_id/Key/渠道校验，
并复查来源和附件；与 URL 结果保留旧地址的精确快照相比，Data URL 是一次新的字节披露，必须重新授权。
结果到期 410；归属失败按现有拒绝语义；附件撤销或文件缺失时返回对应不可用占位和 null URL，
必要时更新本次展示模式，但不修改保存的原始描述、不重新执行模型、不换图或切换 transport。
这也是“精确重放”的明确例外：删除后不为追求字节相同再次发送原图。
服务重启或图片丢失时不从模型重做结果；预期文件缺失标记不可用并记录结构化原因，未知程序错误仍失败，
不以宽泛异常捕获把所有读取失败包装成成功占位。

## 流式、安全与接入方

控制台继续只用私有 URL，任意模型输出的 `data:` 图片仍不被授权。
公开内联模式沿用 06 的“安全文字前缀 + 提交后剩余投影”：提交前只暂存有界规范文本与图片引用，
受 1 MiB 规范缓冲限制；提交后的编码输出受独立内联预算限制，不能把 Base64 算成普通 4 KiB 图片候选行。
按 UTF-8 字符边界拆成每块最多 16 KiB 的 content delta，原生最终 done 可携完整结果，网关与 SDK
必须能处理受限的大事件；不能把 Data URL 拆为语义独立的多张图。错误、取消和断连释放临时编码缓冲。

HTTP/SSE 输出均 no-store；URL 模式的 grant 撤销不适用于已经交付的 Data URL 字节。
日志、审计、异常、普通列表和提交的快照不得包含 Base64，脱敏覆盖 answer 与 images.url 两处。
调用方提交包含 Data URL 的 assistant 历史时，在现有请求大小边界内按 AST 清理图片目标，
只留有界文字占位后传入模型，不解码、不抓取、不把它恢复为本轮可信证据；入站超限按现有规则拒绝。

部分 Markdown 渲染器/CSP 默认拒绝 `data:`。SDK 文档须说明这是 opt-in 的兼容能力，
不能承诺所有 OpenAI 客户端自动展示；不得为此全局放开任意 data URL、SVG 或 raw HTML。
未来视觉输入可以在服务端适配器中复用可信字节编码器，但需要另行启用视觉能力/预算；本次不增加视觉模型调用。

## 验收归属

成功、边界、幂等、删除、历史输入清理及流式场景追加到 [08](08_validation.md#transport-validation)。
本模块随第二阶段发布，与 URL 模式同时设计和验收；当前只增加文档，不实施功能或调用外部模型。
