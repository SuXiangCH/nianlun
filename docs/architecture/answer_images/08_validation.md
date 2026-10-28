# 分模块验收与测试

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md)

## 职责与依赖

保存完整验收清单，按模块划分测试归属，作为分阶段交付门槛。

每份设计的“模块验收”是入口；本文件保存 01–07 及新增 09/10 的成功路径、边界、并发与主要失败场景。

## 计划改动位置

以下是设计约定的测试归属。多个模块共用契约和实现，不建立重复流程。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 扩展测试 | `tests/api_server/test_document_normalization.py` | 01：本地图片解析与不可用状态。 |
| 扩展测试 | `tests/api_server/test_workspace_snapshot.py` | 01：manifest 升级、发布与资源生命周期。 |
| 扩展测试 | `tests/indexing/tree/test_workspace.py` | 01：节点文本和窗口坐标。 |
| 扩展测试 | `tests/agent/test_retrieval_answer.py` | 02：回答输入、图文/纯图和指代。 |
| 扩展测试 | `tests/agent/test_tools_runtime.py` | 02：批次、请求隔离与主/子 Agent。 |
| 扩展测试 | `tests/api_server/test_chat_persistence.py` | 03/04：迁移、原子完成和恢复。 |
| 扩展测试 | `tests/api_server/test_application_publishing.py` | 02/05：v4/v5 发布兼容。 |
| 扩展测试 | `tests/api_server/test_public_runtime.py` | 05/06：授权、幂等和投影。 |
| 扩展测试 | `tests/api_server/test_application_runtime.py` | 05/06：会话归属与运行生命周期。 |
| 扩展测试 | `app/frontend/src/features/chat/ChatView.test.tsx` | 04：正文图片、纯图与失败状态。 |
| 扩展测试 | `app/frontend/src/features/chat/ChatView.sources.test.tsx` | 04：消息级来源、预览和重复资源。 |
| 扩展测试 | `app/frontend/src/api/sse.test.ts` | 04：分块与流完成。 |
| 新增 | `tests/api_server/test_document_image_backfill.py` | 01：补建、维护锁、外部依赖禁用。 |
| 新增 | `tests/api_server/test_answer_images.py` | 03/07：归档、删除、GC、完整性和预算。 |
| 新增 | `tests/api_server/test_public_answer_images.py` | 05/06：grant、刷新和公开流式。 |
| 新增 | `tests/api_server/test_answer_image_transport.py` | 09：内联编码、预算、结果描述兼容、撤销后重放及历史输入清理。 |
| 新增 | `tests/api_server/test_image_blob_store.py` | 10：本地/S3 端口契约、类型、字节完整性、异常及配置边界。 |
| 新增 | `tests/api_server/test_image_blob_lifecycle.py` | 10：上传预约、读取租约、GC claim、迟到 IO 及失败恢复。 |

下列条目保留完整验收要求，不能把自动化通过等同于全部部署项已验证。

## 执行入口与部署边界

从仓库根目录运行 `make lint`、`make typecheck` 和 `make test`；前端在
`app/frontend/` 运行 `npm test`、`npm run typecheck` 和 `npm run build`。
默认测试使用临时数据库和 fake 外部依赖，不调用真实模型。

真实 MinIO/nginx 与旧版本升级演练必须显式启用，前置条件和执行命令见
[基础设施验收说明](../../../tests/acceptance/README.md)。OpenAI SDK 协议测试使用
确定性 ASGI 运行时，入口为 `tests/api_server/test_answer_image_sdk.py`。

当前部署限定单 API 进程/worker 和本地 workspace；S3 存储不自动赋予多主机部署能力。
上线前须在实际环境核对 endpoint、bucket 权限、CA、证书、网关和容量，停止旧进程并备份数据；
升级与恢复遵循 [snapshot 边界](01_indexing.md#snapshot-upgrade)。
维护锁不能识别未接入协议的旧程序，本地 MinIO 验收也不等于真实 AWS S3 或云故障验证。

URL grant 可过期或撤销，Data URL 已交付的字节不能远程撤回；客户端需允许相应 Markdown/CSP。
真实模型的选图质量与用户文档效果需要单独验证，不属于默认自动化测试。

<a id="s13"></a>

## 验收与测试

<a id="s13-1"></a>

### 确定性单元测试

- Markdown 内联、引用式图片、HTML img、转义、中文路径、代码块和重复图片。
- 节点规范化、CRLF、字符窗口与图片引用恰好位于截断边界的情况。
- ASCII、中文和非 BMP 字符覆盖原始窗口连续读取：投影长度变化不改变源 next offset；
  collector 保留原始文本，v4 不变；不完整图片/目标路径不泄漏，重叠重读后才授予完整引用。
- 图片缺失、越界路径、符号链接越界、伪扩展名、超限和摘要不一致。
- 只选实际引用且进入回答上下文的片段；多来源去重、顺序和限额正确。
- 文字引用集合语义保持一致；图片 AST occurrence 按正文位置排序，覆盖代码、数学和链接中的伪标记。
- 受控编号来自当前回答输入目录；仅引用带图片的文字片段不自动插图，父/子 Agent 编号映射不混用。
- 3、10、20 张实际引用图均完整保留；重复资源首次展示、后续跳转且保存全部有效来源。
- 未知/历史编号、任意 URL、内联/表格/HTML 图片、超资源预算都不直接加载，保留明确位置说明。
- 目录截断、历史图片投影截断及来源预览预算都有显式状态；资源预算在请求间隔离，重复资源按唯一摘要计费。
- Markdown 规范化后 occurrence 字符区间正确，格式版本、渲染版本及附件映射可恢复，中文和非 BMP 字符不混淆 UTF-8 字节与字符坐标。
- 显式发图、只发原图、上下文指代、无图和指代歧义；没有文字 citation 也可返回有效显式选择。
- 选择工具拒绝未读图片、跨来源组合、重复项和超量项；失败不覆盖已有选择，子 Agent 不能修改顶层选择。
- 同批多次选图全部拒绝且保留此前状态；交换并行完成顺序不能改变结果，下一批合法调用可修正。
- 同批读取的新图不能被并行选择，下一批才可选择；请求间批次状态与图片能力隔离。
- 实际 sync/async Agent 调用链验证 after_model 冻结计划先于工具执行；缺计划拒绝，
  dedup/取消/loop guard 不绕过预检，不仅单测事后批次标注函数。
- 纯图模式跳过回答模型；归档全部失败时返回说明，图片输出禁用时不能产生空答案。
- 纯图后“第二张图”按最终展示顺序定位；元数据的轮次、图片、来源、图题和 token 上限均生效，
  预算裁剪保持轮次一致，历史引用不直接进入本轮 collector。
- 多张图片图题相同时，按消息 ID、展示序号与完整来源定位区分，不按图题合并；
  来源预览不占答案图片序号，不需要复用历史 `ref_ids`。
- v0/v1、纯文本模型和支持视觉的模型均不增加图片理解调用。
- 新 v3 发布和旧 v2 读取；发布、删除、GC 不遗漏图片目录及其资源。
- 先发布带图 A 再发布/删除 B，A 的目录与全部资源仍被枚举；文件数/序列化字节边界明确拒绝且旧 snapshot 不变。
- 新代码读 v2/v3，旧严格读取器拒绝 v3；首次 v3 写入前停止全部旧读进程的部署演练，
  回滚到 v3 兼容代码及一致备份恢复流程明确，不把维护锁当成旧程序版本探测器。
- 工具契约 v4 的历史发布保持原文字调用，图片请求受理前明确拒绝；v5 支持标记与纯图工具，缓存和主/子 Agent 边界没有混用。
- 历史 prompt 哈希映射到真实保留的 v4 builder/模板，不仅放宽 hash 校验；未知哈希仍拒绝。
  旧控制台会话固定 v4、新会话 v5，checkpoint 不改写；v5 首版 state 可选字段仍为 checkpoint schema 1，
  若未来升 schema 必须单独注册和迁移；WeCom v5 强制禁图，索图返回文字说明而非空成功。
- 旧文档补建的 dry-run、重复执行、缺少资源、中断重跑及与更新/删除并发；原有封存目录和历史消息不被修改。
- 100 个新增目录默认只产生 1 次 revision；混合幂等/不支持/失败文档不占有效批次名额，
  报告以提交成功为准，dry-run 只预测预算与 revision，失败重跑不重复推进。
- Pillow 本地 PNG/JPEG/WebP、伪扩展名、动画 PNG/WebP、GIF、SVG/HTML、截断数据、
  8 MiB+1、4000 万像素+1、解压炸弹、EXIF 方向及 CMYK JPEG；不调用网络或真实模型。
- 独立进程验证服务持共享锁时补建和 dry-run 均拒绝，补建持独占锁时服务在初始化前拒绝，
  两个补建互斥、异常退出释放锁；拒绝路径不触发消息失败标记、调度、迁移或 GC。
- 隔离的补建 service 测试仍覆盖 CAS 冲突和文档删除；真实命令的首版并发边界由维护锁保证，
  不以进程内 snapshot 引用计数通过跨进程资源保护测试。
- fake 外部依赖在补建期间一旦被调用即令测试失败；索引版本不一致时保持 pending，并验证文本降级。

<a id="s13-2"></a>

### 服务与协议测试

- 临时 SQLite 覆盖旧库迁移、原子完成、提交失败、文件孤儿和引用归零清理。
- 使用不同的规范/公开答案验证写入来源：消息与下一轮输入无 token URL；URL 模式 result_json 与公开响应一致，
  Data URL 模式持久化严格的投影描述、公开结果经统一加载器重建，二者均无 Base64 规范历史，
  两份答案和附件/grant 同事务；控制台阻塞、重建 done 与刚提交的历史使用同一最终格式/模式及失败说明。
- 本地后端用可控暂停和独立进程覆盖旧无引用 blob 被复用时与 GC 的两种先后顺序：
  归档先提交则 GC 跳过，GC 先删除则归档重建；最终可用附件对应的文件存在且摘要正确。
- 覆盖相同摘要的并发归档、多图固定锁顺序、GC 扫描后新增引用及回收中断重试；
  文件重建失败不生成可用附件，数据库回滚后的残留资源可幂等清理。
- 文档、知识库、会话删除与 blob/孤儿 GC 不删除、移动或替换共享媒体锁目录及文件；
  用独立进程持锁跨越清理操作，验证锁 inode 不变且竞争者仍不能取得同一独占锁。
  上述文件锁验收适用于本地归档/工作区；S3 对象回收按下述后端验收验证 CAS/租约，不以 POSIX 锁替代。
- 历史消息在文档更新及旧 generation 清理后仍展示旧原图；文档删除后不可再访问。
- 纯图后的指代在会话重载、服务重启后仍可定位，失败/取消轮次不进入图片历史；
  文档更新、删除和 Agent 知识库范围缩小时重新读取受限，不复用旧授权或错误匹配新图。
- 原生 API 同一会话中纯图后“第二张图”读取正确历史；新会话、不同客户端或 `user_id` 不串图。
  过期、中断、checkpoint 缺失及跨归属继续拒绝；v4 发布或图片关闭时历史不意外开启纯图能力。
- OpenAI 调用方历史中的 Markdown 不触发 URL 抓取，不凭自报图片编号访问资源；
  指代不足时走澄清，同一 `user` 的不同调用不自动合并服务端图片历史。
- 用可控暂停覆盖“归档后删除、附件尚未提交”和“附件提交后再删除”两种顺序，验证不会重新激活已删除来源。
- 覆盖知识库删除、grant 刷新与图片读取并发；删除提交后的新读取和刷新失败，已开始传输的响应按约定结束。
- 控制台跨应用、跨会话、跨消息附件拒绝访问。
- `display_in_answer=false` 的可用预览附件出现在控制台阻塞、done 和历史 `images` 中，
  可通过私有端点读取，按来源附件 ID 正确定位，不追加到正文或改变输出模式；
  同一资源同时用于答案和预览时只保留一份附件及全部有效来源。
- 原生 API、OpenAI、公开 run 结果和幂等重放不返回仅来源预览附件的元数据或 URL，
  也不为其签发 grant；答案附件的标准 Markdown 与来源绑定保持不变。
- 原生 API 默认兼容，公开响应不泄漏内部字段；请求开关纳入幂等指纹。
- grant 的过期、刷新、Key 撤销、渠道停用、应用禁用和跨客户端拒绝访问。
- 时间可控地覆盖续聊已过期但结果仍可读、grant 过期可刷新、run 结果到期禁止读取/刷新/重放，
  后台尚未清理亦拒绝；并发清理/刷新不复活访问，过期幂等键不重新执行，旧 run NULL 截止时间不自动删除，
  且旧 run 的图片 grant 签发/刷新明确拒绝。
  清理重试、零引用宽限及控制台无 TTL 的选择与生命周期矩阵一致。
- 使用可识别的 fake token 检查普通运行列表、日志和审计输出不泄漏凭据，
  同时覆盖 `answer` 中的 Markdown URL、`images[].url` 及这些入口的原始响应快照投影；
  提交到版本库的测试快照须脱敏，不把合法 run 结果在临时数据库内保存链接判为泄漏。
  归属校验通过的 run 结果详情必须保留已保存链接和过期时间，SDK 能读取并刷新；
  跨应用、客户端或 `user_id` 的详情请求拒绝，`expires_at` 不作为秘密字段过滤。
- 网关只能开放设计中的精确路径，不暴露管理或文件系统入口。
- 网关使用实际 UUID app/run/grant/attachment ID 校验新增 GET/POST 路径；拒绝多余路径段及错误方法。
  实际 uvicorn 入口捕获 stdout/logger，fake token 不出现且仅 path 的请求指标保留；TestClient 不代替该检查。
- OpenAI 阻塞与流式最终文本一致，图片只在持久化成功后输出，幂等重放不续签。
- 正文“文字 A、图 1、文字 B、图 2、文字 C”在控制台、原生 API、OpenAI 与历史恢复中顺序一致；
  原生及 OpenAI 输出标准原位 Markdown，内部规范答案无 token URL，重复引用不重复嵌图。
  不要求规范标记与标准 Markdown 字节相同；URL 公开阻塞/流式/重放精确等于保存的 result.answer；
  Data URL 在授权/资源不变时由固定描述重建相同内容，撤销后不重放原字节，
  不自动增加示例中的“原图来源”行。
- 在图片语法任意字符边界切块，覆盖 CRLF、未闭合标记、跨行 HTML、引用定义后置、代码围栏、转义和 URL 攻击；
  首次已发安全前缀与最终投影一致，流中不泄漏内部 scheme，不产生外部图片请求。
- 暂存期间心跳和 worker 队列持续消费；缓冲/标记次数超限、断连和取消均释放资源，不死锁或截断后成功。
- 纯图归档全部失败或提交时来源全部失效时，首次阻塞、首次流式和幂等流式重放
  返回同一说明，流式说明恰好一次且位于 finish chunk 前；没有成功空内容或无效图片链接。
- 纯图成功、图文成功及无 token 增量的非空结果覆盖统一展示投影；流式正文不重复，
  已发送前缀不匹配时不成功结束，提交失败和取消不输出成功降级说明或附件。
- OpenAI 阻塞/流式、首次/重放均带相同 Location；SDK 可查询 run 并以原 user_id 刷新图片，跨归属和跨域地址拒绝访问。
- 已允许的浏览器 Origin 携带 `X-Nianlun-Include-Images` 和既有鉴权/幂等头时预检通过；
  成功响应通过 `Access-Control-Expose-Headers` 暴露 Location，浏览器可读取查询地址。
- 保留 SSE 的取消、错误、chunk 边界、结束顺序和前端 AbortSignal 清理。

<a id="s13-3"></a>

### 前端验收

- 无图、单图、3 张及 20 张图、长图、缺失图片、错误状态和消息刷新恢复。
- 按正文位置渲染，重复图跳转到本消息首次位置；图片未进入视口不加载，预留宽高，第三张起不静默折叠。
- message 期间仅安全占位，done 后以完整答案替换并解析附件；默认 URL 过滤不吞合法标记，也不放开其他 scheme。
- 在 ChatView 的真实 ReactMarkdown 渲染点验证 AST/组件接入，不能仅测试 markdown.ts 的字符串处理。
- 纯图结果不出现空文字气泡；显式请求无图或全部归档失败时展示简短说明。
- 来源同号但属于不同消息时，图片与引用仍定位到正确消息。
- 来源预览只在来源面板显示，页面刷新后可恢复；不因获取了私有 URL 而进入正文或纯图结果。
- 移动端无横向溢出，图片加载不推动正文反复跳动，查看器可键盘操作。
- Markdown 中任意外链图片、data URL 或伪造内部地址不能绕过附件清单直接加载。
- 只为已验证附件创建 img；对用户正文、历史正文和来源中的未验证图片使用安全占位。

<a id="transport-validation"></a>

### URL/Data URL 传输验收

- 原生默认及省略 transport 等价于 url；OpenAI 两个请求头与原生参数等价；
  非法枚举、关闭图片配 data_url 拒绝，同幂等键切换模式冲突，正常文字调用不变。
- URL 要求 public base URL，Data URL 不要求公网图片地址；仍验证发布契约、Key、渠道及结果期限。
  客户端仅能访问聊天/run 接口而图片接口不可达时，内联模式仍可取得原图字节。
- 已验证 PNG/JPEG/WebP 的 Data URL 可解码回原始字节且 MIME/摘要一致，标准 Base64 无换行、填充正确；
  重复引用、未知 ref、任意模型 data URL、超限及全部失败遵守同一位置/模式归一化。
- 1 MiB 单图、2 MiB 总原始字节、8 MiB envelope 的恰好边界和超 1 字节，含 N%3 的编码填充分支；
  answer 和 images 中的重复 payload、UTF-8、JSON 转义、OpenAI/run 详情差异均准确计费。
  完整 envelope 超限时无 completed/成功 done；内联超限不自动生成 URL grant。
- result_json 的旧公共快照/URL 快照/inline_projection_v1 严格解码；新描述不是 PublicChatResponse，
  完成返回、run 查询及重放均走统一加载器，未知 storage_format 明确拒绝。
- 第一次及重放授权不变时内联 answer/images 字节一致，预算结果冻结；数据库、规范消息、模型输入、
  应用/ASGI 日志、普通列表及提交的 fixture 无 Base64；公开 DTO 不泄漏内部模板、路径或摘要。
- NULL 结果期限的旧 run 不升级成内联结果；到期 410、不重新执行模型；Key/渠道/来源撤销后重放不发原图，
  并发删除/GC 与内联读取遵守本地媒体锁或 S3 租约及判定时点，不能把已编码内存当成继续披露的权限。
- 正常、纯图、失效、重复图的原生/OpenAI 阻塞与流式最终投影一致；提交前无 Data URL，
  规范缓冲与内联输出预算独立，content 分块受限且可无损拼接；取消/断连释放缓冲，不持锁传输。
- 真实渲染器/CSP 支持与拒绝 data scheme 的客户端各验收一次；默认控制台仍不接受任意 data URL。
  SDK 明确 expires_at=null、内联无刷新接口；URL 结果刷新保持既有行为。
- 调用方回传含 Data URL 的 assistant 历史时，不解码/抓取或传回模型；在入站大小限制内仅保留有界占位，
  超限明确拒绝，不影响正常文字历史和服务端原生会话图片指代。

<a id="blob-storage-validation"></a>

### 文件系统与 S3/MinIO 存储验收

- 同一 BlobStore 契约覆盖本地及 SDK stub：原始字节/大小/MIME 一致，错误分类一致，
  数据库仅保存定位和元数据；切换后端不改变 API 附件形态，URL/Data URL 都能从两种后端读取。
- 默认 filesystem 且无历史 S3 责任时不构造 S3 client、不探测云凭据、不联网；S3 配置缺失或 profile 指纹变更明确拒绝，
  凭据不进入响应/日志/异常。MinIO path-style、标准 AWS endpoint、超时/重试/TLS 配置分别覆盖。
- 存量本地 blob 迁移默认定位不改变原图；更换默认 backend 后旧 profile 仍可读，不自行移动/删除原文件。
  对象 key 不接受客户端路径；公共响应/重放无 endpoint/bucket/key/凭据，客户端不需连接 MinIO。
- 同一 profile 定位不可变，包括只剩 deleted 凭证时；新 profile 可切换默认值，旧 profile 继续读取/清理。
  切换后重启、缺少旧凭据/角色引用、指纹不匹配分别验证；迟到 PUT 仍在旧 bucket 精确回收，不误删新 profile 对象。
  profile 映射拒绝未知字段/缺失默认项/backend 冲突；平铺单 profile 身份在重启后稳定，不覆盖已登记定位，配置密钥不入库。
- uploading 唯一预约后才可 PUT；相同摘要并发只有一个活动 uploading/ready blob，未 ready 的对象不能挂附件。
  上传成功但完成事务失败不发成功附件；取消、网络超时后真实已写入、worker 崩溃均有可追踪孤儿。
- owner 取消主动退役，崩溃后租约失效可由新请求/worker CAS 退役；与续租竞争只有一个成功。
  不推进 24 小时宽限也可预约新 key；多个竞争者只有一个活动预约，单请求替代次数有界。
  旧 owner 迟到 PUT/续租/提交不激活 retired，不修改新 blob；旧对象仍等待宽限后清理。
- ready 对象复用与 GC 两种顺序：读取租约/提交引用先成立则 GC 跳过，GC 先 CAS deleting 则新引用拒绝，
  可立即用新 UUID/key 重建；旧删除失败或迟到 DELETE 永不阻塞/命中新 key。
- 确认缺失/大小或摘要不符的 ready 即使关联历史附件也能 CAS 退役；附件不可用、解绑、grant 撤销同事务。
  新请求持有效 snapshot 且原始摘要一致时可重新归档；历史附件不自动改绑，重放不触发修复，来源撤销不恢复。
  并发复用/读取最终检查拒绝退役对象；403/限流/超时不误判损坏，原始字节不可验证时不创建替代附件。
- 租约续租/丢失、claim 过期后续领、删除失败/中断重试、PUT 迟到发生在 DELETE 后，
  验证旧预约不能转 ready、回收凭证能再次发现并删除旧对象；不把租约 TTL 当成对象已不存在的证据。
- 读取全量验摘要/大小后才可发字节，伪 ETag/错误 metadata 不绕过校验；
  来源/Key/渠道在远端 GET 期间撤销后不可输出字节。读租约阻止 GC，断连/取消关闭远端 body 和临时文件。
- S3 IO 不持数据库写事务，不在 async route 阻塞；重试受限，错误不回落本地目录，不调用模型补图。
- versioning 关闭/开启分别覆盖删除；开启时精确版本读取/回收，delete marker 不是物理回收成功。
  普通测试使用临时 SQLite 与 fake/stub；显式 MinIO 集成验收独立临时 bucket 中的上传、重启恢复、读取及回收，
  不访问用户 bucket、不默认启动外部服务、不把集成未运行视为已验证真实 S3 兼容性。

测试使用小型图片 fixture、临时目录、临时 SQLite 和 fake 模型，不调用真实模型或 MinerU。
验证至少包括相关 Python API/Agent/索引测试、`make lint`、`make typecheck`、
前端相关 Vitest、`npm run typecheck` 和 `npm run build`，以及桌面与移动端浏览器检查。
