# 控制台接口与聊天界面

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md)

## 职责与依赖

在正文对应位置展示原图，处理流式、历史恢复、窄屏、查看器和错误状态。

使用 [Agent](02_agent.md) 的规范标记和 [归档模块](03_storage.md) 的已提交附件。与 [公开流式](06_openai.md) 共享语法边界，私有资源入口遵守控制台部署边界。

## 计划改动位置

以下是设计约定的改动位置。多个模块共用契约和实现，不建立重复流程。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 修改 | `app/api_server/apis/v1/schemas.py` | 附件、answer_format_version 和 answer_output_mode 严格 DTO。 |
| 修改 | `app/api_server/apis/v1/chat/api.py` | 附件响应、私有图片读取与完整归属校验。 |
| 修改 | `app/api_server/common/sse.py` | ready/done 携带新增字段，保留原事件与错误/结束语义。 |
| 修改 | `app/api_server/services/chat_service.py` | 持久化后提供最终正文和附件，历史使用同一投影。 |
| 修改 | `app/frontend/src/types.ts` | 同步 snake_case 字段及旧消息默认值。 |
| 修改 | `app/frontend/src/api/client.ts` | 在现有客户端集中封装附件响应和资源访问。 |
| 修改 | `app/frontend/src/api/sse.ts` | 解析格式与最终附件，保留分块和 AbortSignal 清理。 |
| 修改 | `app/frontend/src/features/chat/markdown.ts` | 导出受控图片 AST 转换及流式安全边界辅助函数。 |
| 修改 | `app/frontend/src/features/chat/ChatView.tsx` | ReactMarkdown 接入 AST 转换/附件组件，临时占位、done 全文替换与消息级来源定位。 |
| 新增 | `app/frontend/src/features/chat/AnswerImage.tsx` | 懒加载、尺寸预留、不可用占位与重复图跳转。 |
| 新增 | `app/frontend/src/features/chat/ImageViewer.tsx` | 放大、多图切换、Esc 与焦点恢复。 |
| 修改 | `app/frontend/styles.css` | 正文图片、长图、查看器和移动端样式。 |

<a id="s8"></a>

## 控制台 API 与 SSE

<a id="s8-1"></a>

### 响应契约

为 `ChatResponse`、`ConversationMessageResponse` 和 SSE `done` 增加 `images`，默认为空。
同步返回 `answer_format_version`，新图片正文使用 2；旧消息和无图片能力调用保持兼容格式。
`MessageSourceResponse` 增加图片附件 ID 列表；同一个附件可关联多个来源。
完成结果增加 `answer_output_mode`，取值为 `text`、`images`、`text_and_images`，旧记录默认 `text`。
图片模式下 `answer` 仍为字符串，允许为空；前端和 API 不能再仅凭 answer 非空判断成功。

控制台 `images` 是本消息的完整有界附件清单，包含答案图片及已成功归档的来源预览图片。
`display_in_answer=true` 表示答案引用，`false` 表示仅用于来源预览；同一资源同时承担两种用途时
复用同一附件并设为 true，保留全部有效来源关联。两类可用附件都返回私有 `url`，
均可通过下述 content 端点读取；预览附件不因未出现在正文中而被拒绝。
控制台固定使用私有 URL，不向 SSE 或历史 JSON 内联 Base64。客户端只需访问 Nianlun 图片接口，
不需访问底层存储；公开 API 的可选内联模式见 [09 图片传输](09_transport.md)。
来源面板通过 `MessageSourceResponse` 中的附件 ID 从同一 `images` 清单查找资源，
不自行拼接路径，也不把预览附件追加到正文、计入答案图片顺序或改变 `answer_output_mode`。
该规则同时适用于阻塞响应、SSE `done` 和会话历史，公开 API 的附件过滤另见
[原生 API](05_public_api.md#s9-1)。

示例为拟议的 envelope 内部字段，不表示当前接口已支持：

```json
{
  "answer": "系统包含三个主要模块。[2]\n\n![图 3 系统架构](nianlun-image:img_01)",
  "answer_format_version": 2,
  "answer_output_mode": "text_and_images",
  "images": [
    {
      "id": "attachment_01",
      "ref_ids": ["img_01"],
      "citation_ids": [2],
      "caption": "图 3 系统架构",
      "width": 1200,
      "height": 800,
      "display_in_answer": true,
      "status": "available",
      "url": "/api/v1/apps/app_01/conversations/conversation_01/messages/message_01/images/attachment_01/content"
    },
    {
      "id": "attachment_02",
      "ref_ids": ["img_02"],
      "citation_ids": [2],
      "caption": "图 4 部署示意",
      "width": 1000,
      "height": 600,
      "display_in_answer": false,
      "status": "available",
      "url": "/api/v1/apps/app_01/conversations/conversation_01/messages/message_01/images/attachment_02/content"
    }
  ]
}
```

`status` 首版限定为 `available`、`unavailable`、`deleted`；不可用时 `url=null`。
附件 DTO 同步提供可信 `orientation`（默认 1），与原始 width/height 一起确定实际展示宽高比，
避免带 EXIF 旋转的图片加载后改变预留布局；旧附件缺省按 1 处理。
上述地址由响应转换层构造，不永久存进规范正文或附件位置字段，也不把 blob 路径返回客户端。
`occurrences` 及内部坐标用于服务端一致性校验，公开与前端 DTO 按实际需要白名单投影，
前端主要通过逻辑 `ref_id` 查本消息附件，不根据客户端自报位置或编号取得新权限。

<a id="s8-2"></a>

### 图片读取

拟新增 `GET /api/v1/apps/{app_id}/conversations/{conversation_id}/messages/{message_id}/images/{attachment_id}/content`。
它只属于控制台私有入口，遵守现有私网部署边界，不把它当成已具备用户级鉴权的公网接口。
服务端完整校验应用、会话、消息、附件的关系和资源状态；跨归属或已删除统一返回 404。
有效来源检查遵循 [写入一致性](03_storage.md#s7-3)/[删除与撤销](03_storage.md#s7-4) 的事务与访问判定规则，不以 snapshot 的读引用代替删除状态校验。

成功返回图片字节及正确的 `Content-Type`，不是 JSON envelope；错误保留现有错误 envelope、
状态码与 `X-Request-Id`。使用 `Cache-Control: private, no-store` 和 `nosniff`。

<a id="s8-3"></a>

### 流式行为

继续使用现有 ready、message、done 和 error 事件，不新增图片二进制或可访问链接增量事件。
启用图片的响应在 ready 声明 `answer_format_version=2`。message 流的是规范 Markdown，
前后端按同一受控语法处理；完整逻辑图片节点只显示不访问资源的占位，实际链接仅从 `done.images` 取得。
流式片段可能把图片语法拆开，渲染层必须屏蔽未闭合图片片段；在确定行类型前暂存有界的候选行，
不能让 `nianlun-image:`、原始 URL 或半截 Markdown 闪到页面并触发请求。
只允许独立图片行使该边界可实现；超过图片行长度上限后按无效标记处理，不能无限缓冲。
代码围栏及普通文字中的同形语法按文字显示，不能被图片转换器误吞。

消息和图片附件成功持久化后，done 返回最终规范 `answer`、格式、输出模式及 `images`。
`ChatService` 必须用归档完成结果重建 done，不继续透传 runner 的原始 done；
chat route 的白名单字段同步更新，阻塞响应、done 和刚提交后的历史投影保持一致。
repository 可保持返回 None，由 service 使用已知消息 ID 和已提交完成结果构造严格 DTO；
也可返回内部提交结果，但不要求 repository 依赖 HTTP 响应类型。
前端以这一完整结果替换临时正文，再原位解析附件；不能把最终答案再次追加到已有流。
暂时未知编号在完成校验前不创建 img。取消、error 或没有 done 时仅保留临时文字及失效占位，
不把未提交图片当成正式历史。纯图结果在完成前显示生成状态，完成后展示有序图片集合。
前端在 Markdown URL 默认过滤前识别逻辑标记并替换为安全附件组件，不能全局允许自定义 scheme。

<a id="markdown-integration"></a>

#### Markdown 接入位置

`markdown.ts` 提供转换函数，实际挂载点是 `ChatView.tsx` 的 ReactMarkdown 实例。
在默认 URL 过滤前，通过 AST 转换把已验证逻辑节点转为受控附件节点，由组件映射渲染 `AnswerImage`；
只接受本消息附件映射，不把模型给出的目标透传为浏览器 src。普通图片节点一律安全占位。
可使用局部受限的 `urlTransform` 配合组件校验，也可由 AST 直接转换为附件节点；不强制某个 API，
但必须测试默认 URL 过滤不会吞掉合法标记，且任意 URL、data URL 和伪造私有地址不能绕过清单。
纯图结果复用同一附件组件与顺序，不伪造正文 URL。

页面刷新、切换会话和历史恢复时，通过服务端消息响应恢复同一份附件。
本地缓存只作展示缓存，不能作为图片权限或归属的依据；旧缓存缺少 images 时按空列表处理。

<a id="s4"></a>

## 用户体验

<a id="s4-1"></a>

### 答案区域

文字答案保持当前排版，图片作为正文中的独立块紧接相关段落，不另设重复的末尾图片区。
每张图展示图片、原文图题和来源编号，例如“图 3 系统架构 [2]”。没有原文图题时使用
“原文图片 [2]”，不由纯文本模型补写图中含义。

桌面端单图最大展示宽度建议为 560px，多图按正文位置纵向排列；移动端使用可用宽度。
图片保持原始宽高比，预留稳定尺寸，长图限制预览高度并提供点击放大。
临近视口才加载字节，不把大批图片同时解码。点击图片可放大、查看原图并定位来源。
不增加外层卡片、常驻工具栏或解释性说明；不会在第三张图起自动折叠或省略。

普通问答在完成阶段由服务端按回答 citation 补入被引用片段的原图，放在对应完整 Markdown 块之后；已有显式图片引用的资源不重复补入。未引用的检索片段仍只提供来源预览。没有有效 citation 或图片标记时只展示文字。
多处引用同一片段时，补图结合原图位置及前文内容选择相关的回答块，不集中放在首次引用后；无法匹配时放在该来源最后一处引用后，保持原图先后顺序。列表、表格和引用块保持完整。
这不代表模型已经理解图中内容。用户明确索要图片时走 [显式发图与只发原图](02_agent.md#s6-4)；“只发原图”不渲染空文字气泡。
同一资源第一次出现时展示完整图片，后续引用显示“查看前图”并跳转到首次位置；来源关联仍全部保留。
源文件已归档且权限有效时，查看原图的链接对应本消息的原始字节，不是工作区相对路径或临时链接。

<a id="s4-2"></a>

### 来源与查看器

- 点击来源编号，定位到该消息对应的来源；不能跳到另一条消息的同号引用。
- 来源展开后显示本次实际读取范围内的图片；未被正文引用的来源图片不自动附到答案。
- 来源原始 Markdown 中的图片节点不重复渲染；图片仅通过该来源的 `image_attachment_ids` 从消息附件清单展示，避免内部相对路径产生误导的不可用占位。图片代码示例仍保留为文字，附件实际不可用或加载失败时才显示失败占位。
- 查看器支持放大查看、关闭、Esc、焦点恢复；需要多图切换时使用前后箭头。
- 加载失败保留占位和“图片暂不可用”，不影响文字答案及其他图片。
- 不显示服务器路径、存储摘要、generation 等内部字段。

<a id="s4-3"></a>

### 没有图片时

普通文本问答不展示空区域或“暂无图片”提示，布局和交互保持现状。
用户明确索要图片却没有找到、或图片已失效时，返回简短的结果说明，不能显示空白成功消息。

## 模块验收

### 知识库文档预览

知识库阅读器直接展示已发布原文的图片，不经过 LLM，不应用聊天答案的图片数量限制。

- `GET /api/v1/knowledge-bases/{kb_id}/documents/{document_id}/preview` 返回原始 `content`、仅用于渲染的 `rendered_content`、`content_version` 和严格的 `images` 清单。
- 原文中的 Markdown、引用式图片及 HTML `img` 被投影为 `nianlun-document-image:{id}`，保持原有换行数，避免影响索引树行定位；源码视图仍使用未经替换的原文。
- `GET /api/v1/knowledge-bases/{kb_id}/documents/{document_id}/images/{image_id}/content?content_version={version}` 只读取该版本、该文档目录中登记的图片。文档更新后，旧链接在快照仍保留时读取旧图片；文档删除或快照过期时返回 404。
- 预览沿用文档快照中的原始资源，它们目前保存在 workspace 的不可变图片目录中；聊天附件独立支持 filesystem/S3/MinIO，配置附件存储不会改变原文预览入口。
- 图片字节通过现有受限文件读取器验证大小、摘要、格式与尺寸；不返回文件路径、存储凭据或 blob 定位信息。成功响应使用图片 MIME、`private, no-store` 和 `nosniff`；错误沿用 envelope 与 `X-Request-Id`。
- 前端只接受清单中的受控图片 URL，懒加载并预留尺寸，支持点击放大、前后切换、Esc 关闭与焦点恢复。外部链接、未登记图片和加载失败显示占位，不阻塞正文；图片目录异常有日志且正文仍可阅读。

实现位置：`document_preview_service.py`、知识库路由、`DocumentReader.tsx`、`DocumentImage.tsx`；大图查看器由知识库与聊天共用 `components/ImageViewer.tsx`。

Python API 契约和跨归属测试；扩展 ChatView.test.tsx、ChatView.sources.test.tsx、ChatView.conversations.test.tsx、ChatView.stop.test.tsx、ChatView.strictmode.test.tsx 及 src/api/sse.test.ts。浏览器检查桌面/移动端无图、单图、多图、长图和失败状态。

具体边界场景见 [完整验收清单](08_validation.md)。
