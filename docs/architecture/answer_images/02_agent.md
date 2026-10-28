# 检索引用与 Agent 输出

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md)

## 职责与依赖

让模型从本轮已读取证据中引用原图，覆盖图文、纯图和多轮指代。

消费 [图片目录](01_indexing.md)，输出规范 Markdown、来源身份、有序 occurrence 和输出模式，由 [归档模块](03_storage.md) 校验保存。历史定位读取已提交附件元数据。

## 计划改动位置

以下是设计约定的改动位置。多个模块共用契约和实现，不建立重复流程。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 修改 | `nianlun/knowledgebase/core.py` | get_line_content 按规范化节点字符窗口返回有界图片引用。 |
| 修改 | `nianlun/agent/contracts.py` | 扩展端口、片段/结果/历史类型与请求策略，工具契约推进至 v5。 |
| 修改 | `nianlun/agent/lead_agent/runner.py` | 请求级 collector、编号映射、选择状态与结果归一化。 |
| 修改 | `nianlun/agent/lead_agent/factory.py` | 按 v4/v5 与本次图片能力构造工具集，缓存键隔离。 |
| 修改 | `nianlun/agent/lead_agent/agent.py` | 接入纯图选择工具和执行路径。 |
| 新增 | `nianlun/agent/middleware/answer_image_selection_middleware.py` | 执行前批次裁决、冻结候选，按 tool call 拒绝非法选择。 |
| 修改 | `nianlun/agent/middleware/__init__.py` | 导出选择中间件，由 factory 按契约与能力注册。 |
| 修改 | `nianlun/agent/tools/__init__.py` | 注册选图工具、读取结果投影，build_tools 接收契约版本与图片能力。 |
| 修改 | `nianlun/agent/lead_agent/routing.py` | 接收有界图片历史，支持发图与指代澄清。 |
| 修改 | `nianlun/agent/lead_agent/prompt.py` | 受控编号、纯图意图和视觉事实边界。 |
| 修改 | `nianlun/agent/retrieval_answer/graph.py` | 纯图跳过回答模型；图片目录与回答输入片段保持一致。 |
| 修改 | `nianlun/agent/retrieval_answer/input_messages.py` | 计入 token 预算的证据、图片目录与历史投影。 |
| 修改 | `nianlun/agent/retrieval_answer/state.py` | 独立保存输出模式、显式选择与展示来源。 |
| 修改 | `nianlun/agent/retrieval_answer/prompt.py` | 只输出受控图片标记并遵守本轮预算。 |
| 修改 | `nianlun/agent/subagents/result.py` | 主/子 Agent 只交接有界图片引用。 |
| 修改 | `nianlun/agent/subagents/tools.py` | 允许列表与局部编号映射，子 Agent 不修改顶层选图。 |
| 修改 | `app/api_server/services/chat_service.py` | 从已完成消息构建受信图片历史。 |
| 修改 | `app/api_server/services/application_invocation_service.py` | 原生会话归属校验后加载历史；OpenAI 使用调用方历史。 |
| 修改 | `app/api_server/services/application_release_service.py` | 发布校验显式兼容 v4/v5 与图片能力。 |
| 修改 | `app/api_server/services/application_service.py` | 发布/控制台 runtime 传递契约与能力，缓存键含 prompt bundle。 |
| 修改 | `app/api_server/database/models/chat.py` | 控制台会话保存工具与 prompt 契约标识。 |
| 修改 | `app/api_server/database/migrations.py` | 已有控制台会话标记为 v4，新会话显式写入 v5 契约。 |
| 新增 | `nianlun/agent/prompt_registry.py` | 解析受支持工具/prompt 组合及固定模板，拒绝未知组合。 |
| 新增 | `nianlun/agent/prompts/v4/` | 随代码保留升级前可运行发布对应的提示词构造器和模板。 |
| 新增 | `nianlun/agent/tools/answer_images.py` | 严格的纯图选择工具，不读取图片字节或访问 Web/数据库。 |
| 新增 | `nianlun/agent/answer_images.py` | 受控 Markdown AST 提取、引用校验、去重和 occurrence 生成。 |

<a id="s6"></a>

## 检索与附件选择

<a id="s6-1"></a>

### 检索结果

在正文项和 `RetrievedSnippet` 上增加有界、可选的 `images` 列表，旧结果默认为空。
Agent 核心只传资源标识、原文图题、位置和版本，不传文件字节、访问凭据或 HTTP 地址。
具体文件读取由 Web service 层通过稳定端口完成，不让 Agent 依赖 SQLite 或 FastAPI。
新增输出 transport 只影响 service 的公开展示投影，不改变本层允许集合或开启视觉理解。
调用方回传 Data URL 图片历史时，Web 准入层按 [09](09_transport.md) 清理图片目标后再交给模型；
不因服务端曾输出它就把任意回传 Base64 当作可信图片或再次加入上下文。

同一次请求中的 collector、去重和图片集合保持请求级隔离。子 Agent 只交接有界的
图片引用，不交接 Base64、文件句柄或整个文档的图片目录。

v1 回答输入使用明确的文本证据投影及有界图片目录，不把存储元数据整体序列化给模型。
每个目录项提供请求级 `ref_id`（如 `img_01`）、原文图题、尺寸、状态及关联 citation ID；
服务端私有映射保存完整来源身份。正文中的原图片路径投影为这个编号，避免模型复制本地路径。
请求级编号由 collector 分配且在本轮稳定；子 Agent 的局部编号须由父级映射后才可进入主回答。
先按文本预算选择片段，再生成允许图片集合和目录并计入同一输入预算；不能把未入选片段的图片
另塞进回答目录。目录裁剪、候选预算及其截断状态须显式反馈，不能让模型以为已经枚举全部原图。
本阶段不修改事实依据规则，图片像素仍未作为模型可读取的事实输入。

<a id="line-content-projection"></a>

### 读取与模型投影契约

沿用源节点坐标，不把 v5 的字符窗口改成投影坐标。知识库端口仍返回 JSON 字符串，
由严格的内容项类型解析；源正文及图片位置只由持有 snapshot 的知识库生成。
v4 保持原字段、原文本和工具描述；v5 在 collector 内完成源片段登记，再构造模型可见结果：

| 字段或对象 | 坐标及用途 |
| --- | --- |
| 源内容项 `text` | `sanitize_text` 后节点正文的窗口，collector 私有使用，不原样发送给 v5 模型 |
| `char_offset/char_limit/total_chars/next_char_offset` | 全部基于源节点 Python 字符坐标；next 仍是源窗口结束位置，不从投影长度推算 |
| 模型内容项 `text` | 将窗口内图片目标替换为受控编号后的文本，长度可与源窗口不同 |
| 模型内容项 `offset_basis` | 固定 `source_node`；工具描述明确下一次调用须使用返回的 next，不累计 text 长度 |
| `images[].source_start/source_end` | 图片语法在源节点内的半开字符区间，供身份与读取范围校验 |
| `images[].text_start/text_end` | 本次模型可见 text 内完整标记的半开字符区间，用于说明图片所在位置 |

源片段身份和 citation 去重使用原文、文档/节点及原窗口；请求级 ref 由完整图片身份稳定分配，
编号和投影长度不参与源片段去重。`RetrievedSnippet` 保留源定位并增加有界图片引用；
进入 v0 工具输出、v1 回答输入和子 Agent 交接时，统一通过投影函数生成模型可见文本，禁止直接序列化私有映射。
v1 先选源片段再生成投影和目录，投影后的实际文本及目录共同计入输入预算，必要时移除整片段并重建允许集合。

先在完整节点上确定语法及图片目标，再按源窗口取片段。只有完整语法落入窗口的图片才可登记允许 ref。
与窗口相交但未读完整的语法、图片引用定义中的目标、HTML img 及不可用目标都按解析位置遮蔽为无链接占位，
不暴露半截路径，不分配越过窗口的可用 ref；下一次仍从源 `next_char_offset` 继续。
窗口连续读取覆盖完整源文，但跨边界图片需用重叠窗口重新读取完整语法才可选中，不自动授权拼接片段。
原文到投影的区间映射与最终答案的 occurrence 是不同对象；后者仍从最终规范答案重新解析。

<a id="s6-2"></a>

### 正文图片引用与校验

复用 Markdown 图片节点作为受控协议，只允许以下请求级逻辑目标：

```markdown
系统由三个模块组成。[2]

![图 3 系统架构](nianlun-image:img_01)

处理步骤如下。[5]

![图 4 处理流程](nianlun-image:img_02)
```

`nianlun-image:` 不是 HTTP 地址，不可被浏览器抓取，也不需要注册系统 URL scheme。
它只在服务端及前端 Markdown AST 适配器中识别。图片必须是顶层独立段落中的单一节点；
首版不支持放入标题、表格、链接或 HTML。代码、数学和普通链接中的同形文字不作为图片。
图题最终取可信目录中的原文值，模型写入的 alt 不作为新增事实或可信图题。

输出解析与引用提取共用表格、公式和代码边界；源位置映射保留表格单元格和转义分隔符的字符坐标。
安全清理只保留允许的静态文字/表格 HTML 元素与属性；其余元素、样式、事件或资源载入属性转为不可用说明，
避免 `image` 别名、SVG/MathML 图片、`srcset`、背景图或预加载绕过 Markdown 图片校验。
这不是提供给调用方嵌入任意浏览器 HTML 上下文的通用消毒器；下游仍应使用其 Markdown 渲染器的安全配置。
替换 HTML/引用定义后再次解析最终正文，直到结构稳定；后续解析只保留此前按精确位置授权的附件节点，
不能让清理过程中新暴露的标记获得授权。所有轮次新发现的图片累计计入 occurrence 预算，图题替换后的正文也重新校验大小。

完成时解析最终 Markdown AST，按源文位置生成有序图片 occurrence：

1. 每个 `ref_id` 必须属于本请求实际已读的允许集合，v1 还必须关联至少一个进入回答输入的片段。
   服务端校验知识库、文档、generation、内容版本及窗口，不能凭模型自报身份扩大集合。
2. 文字 `[2]` 仍是文字证据引用；图片标记本身是图片的显式引用，不要求旁边再写一次 citation。
   展示来源由目录映射取得；不得把任意 citation 所在片段的图片都当成已被答案引用。
3. 按首次图片出现顺序归档和排序；同知识库、同摘要去重资源，保留全部合法来源绑定。
   后续重复 occurrence 保留位置并解析为指向首次图片的内部跳转，不重复解码和签发 grant。
4. 未知编号、历史编号、越权编号和任意 URL 图片一律不加载，转换为该位置的不可用说明，
   记录结构化校验原因；不能猜测相近编号、转读新版本或让前端直接请求原始 URL。
5. 超资源预算、归档失败或来源失效也保留对应位置及原因；原有文字继续，不能静默截去后续图片。
   纯图无有效结果时按 [显式发图与只发原图](02_agent.md#s6-4) 返回说明。未知程序错误仍失败，不伪装为普通资源不可用。

规范答案 `answer` 保存受控标记，不含真实访问链接。图片清单增加 `ref_ids` 与
`occurrences`（按 Markdown 源字符区间、首次/重复类型），在完成事务中一起保存。
不依赖前端猜测段落编号，也不另存一份可独立修改的富文本正文；修改、去重或失效转换后，
必须从最终规范答案重新计算区间。历史读取保持原顺序，只更新附件可用状态。
原生公开 API 和 OpenAI 的标准 Markdown 展示投影见 [发布 API 与公开图片访问](05_public_api.md#s9)/[OpenAI 兼容输出](06_openai.md#s10)。
逻辑编号只在该条消息有效。下一轮输入先把历史标记转换为带消息身份的图片历史描述，
不能把上一轮 `img_01` 原样带入新的允许集合；公开历史中的真实图片链接同样不成为本轮资源引用。
图片目录、正文格式、渲染规则及请求预算的版本随完成结果保存，保证历史解释与首次输出一致。

继续沿用 `extract_citation_ids` 校验文字引用；有序图片提取与文字引用共享现有 Markdown、
代码、数学和 HTML 处理边界，按 AST 源位置排序，不用全局字符串替换或编号排序。
普通图文回答由服务端根据有效文字 citation，在对应顶层段落、完整列表、表格或引用块后补入该片段登记的原图；同一资源只自动插入一次，模型显式引用的资源不重复补入。补图仍受实际回答输入 citation 集合及现有图片预算约束，不把代码、链接或公式中的同形编号当成引用。
插入位置优先参考原文：用图片 `source_start/source_end` 减去片段 `char_offset` 定位，提取前一张图片之后、当前图片之前最多 1600 字符的正文，与引用该来源的回答内容块匹配。使用规范化后的英文词与中文相邻字对计算文字重合度，不调用模型、不读取图片像素；不确定时放在该来源最后一处引用后。同一来源图片保持原文顺序，连续图片继承前文位置，不拆开列表、表格或引用块；因此重写后的回答尽量保持内容关系，不承诺逐行对齐。来源正文和索引坐标不改写。
没有有效正文 citation 或图片标记、不检索、澄清或执行失败时，不自动附图。纯图选择保持原有顺序，不经过文字 citation 补图。

<a id="s6-3"></a>

### 与视觉能力的关系

`supports_vision` 不参与图片展示开关，也不因展示成功而改变。
纯文本模型可以基于图题和邻近正文引用来源，用户随后查看原图；它不能凭图片路径解释图中数据。
后续视觉理解功能可复用目录和版本标识，但须另行定义输入预算、证据规则和失败语义。

<a id="s6-4"></a>

### 显式发图与只发原图

增加 `select_answer_images` 工具，由已有检索 Agent 调用，不增加独立的分类或选图模型。
工具可增加现有 Agent 的工具轮次；“不增加独立模型调用”不承诺总 token 或轮次恒定。

该工具只承担纯图输出，图文回答直接在最终正文使用 [正文图片引用与校验](02_agent.md#s6-2) 标记，不再保留另一套
`text_and_images` 选图优先级规则。拟议参数为严格结构：

```json
{
  "mode": "images",
  "image_refs": ["img_01", "img_02"]
}
```

- `mode` 仅允许 `images`；`image_refs` 非空，上限取本请求冻结的 [文件类型、限额与错误](07_policy.md#s11) 资源策略，不写死为两张。
- 重复的 `ref_id` 视为参数错误；不同引用指向同图时最终摘要去重仍遵循 [正文图片引用与校验](02_agent.md#s6-2)。
- 只用于用户明确要求只发图；需要文字解释时按正常回答流程生成带标记的正文。
- 工具只能从本轮 `get_line_content` 已读取并登记到 collector 的图片中选择，
  按请求级 `ref_id` 解析完整来源身份，不接受路径、URL、Base64 或自报文档版本。
- 历史图片编号仅用于理解用户指代；“把刚才的图发我”等请求仍需重新定位并读取，
  图片已删除或当前内容已变化时不能直接复用历史编号。无法确认指代时使用现有澄清流程。
- 所有条目校验成功后原子替换本轮显式选择；失败返回工具错误，不部分写入、不扩大候选集合。
- 纯图选择保持提交顺序，不再通过文字 citation 自动追加其他图片。
- 图片选择工具只交接资源标识，不读取图片字节，也不推断图中内容。

并行工具调用必须有确定的执行规则：

- 每个模型返回的工具批次最多允许一次 `select_answer_images`。执行前按该批次完整调用列表
  统一预检；若出现多次，全部选图调用返回明确的工具错误，保留此前选择，不采用先完成者或最后完成者。
  其他合法的只读检索调用仍可执行，Agent 可在下一模型批次修正。
- 选择只能使用本批次开始前已登记的图片，预检时冻结可选身份集合；
  不能依靠同批并行 `get_line_content` 的完成时机使选图成功。先读图、下一批再选择。
- 后续批次的合法选择可原子替换此前选择；每次均重新校验请求能力和条目身份。
  批次计划、允许集合与选择状态均为请求级，通过稳定的批次和 tool call 标识关联，不放入共享 runtime。

<a id="selection-middleware"></a>

执行落点为新增 `AnswerImageSelectionMiddleware`，不把预检放入事后的 `annotate_tool_call_batches`：

- `after_model` 在工具执行前检查最终 AI message 的完整调用列表，在 collector 锁内冻结允许集合，
  生成以批次和 tool_call_id 为键的不可变计划；多次 select 的批次将全部 select 标为拒绝。
- `wrap_tool_call/awrap_tool_call` 在调用 handler 前读取该计划，校验能力和冻结身份；
  缺失计划也拒绝选择，不回退到实时 collector。合法选择原子提交，读取工具可继续登记下一批候选。
- factory 的现有列表在 `RetrievalLoopGuardMiddleware` 与 `RetrievalDeduplicationMiddleware` 之间插入该中间件，
  保留取消、错误处理和 Clarification 的相对顺序。取消及 loop guard 的硬拒绝优先，任何拒绝均不修改选择。
  两种运行模式都须验证 after_model 完成后才进入工具、冻结集合在整个批次不变；不能仅以列表顺序推测钩子行为。
- v4 或请求图片能力关闭时不注册选择工具/中间件。`tool_call_batches.py` 继续只负责运行后的展示标注。

v1 将 `answer_output_mode` 和 `selected_answer_images` 作为有界的独立结果字段，
不通过一句自然语言“检索完成”传递图片选择。普通证据片段不因缺少文字 citation 被整体剔除：

- `images` 模式跳过独立回答模型，允许规范 `answer=""`；图片来源通过附件的 citation ID 保留。
  `answer_input_citation_ids` 保持为空，另用 `display_image_citation_ids` 记录展示来源，
  不能伪称这些片段已进入回答模型。
- 未调用工具时执行正常回答节点，所用图片必须来自该节点实际输入目录。
  `text_and_images` 是最终结果模式，由正文文字和已校验图片计算，不作为独立选图工具参数。

v0 使用同一请求级选择状态，由最终结果归一化层输出相同字段，不把候选轮次正文作为图片选择依据。
纯图选择成功后不向用户转发检索总结或模型补写的候选正文；只提交确定的有序图片集合。
更新空答案检查：只有有效显式 `images` 选择存在时，才允许继续完成空文字结果。
归档和最终权限校验后若图片全部不可用，返回简短的同语言说明，不能把空文字加空图片标记为成功内容。

控制台默认允许图片输出；原生 API `include_images=false`、OpenAI 未启用图片请求头时，
不向 Agent 开放此工具或图片输出能力。此时明确发图请求只能返回文字形式的能力提示，
不能产生调用方无法接收的空答案。公开接口启用后才允许纯图片结果。
该能力开关放在请求上下文，工具执行时再次校验；不能通过修改共享 runtime 的可变状态开关工具。
如果构建的工具集或提示词因开关不同而缓存，缓存键必须区分该能力，避免并发请求互相影响。

WeCom 在本阶段强制 `image_capability=false`，由 `prepare_channel` 到请求上下文和工具执行边界共同落实。
v5 发布也只走文字能力，不注册选图工具、不输出内部图片标记、不归档附件或签发 grant；
用户索要图片时返回当前渠道不支持图片的文字说明，不能以空正文表示成功，也不把发布契约冒充成 v4。

两个 Agent 版本的主工具集、v1 检索工具事件列表及 deep-search 子 Agent 的允许列表都要明确更新。
选择工具仅由主检索流程持有：子 Agent 可返回已读图片引用，但不能修改顶层回答的最终附件选择。
相应提示词需覆盖发图意图、可用编号、纯图模式和图片不可作为已理解事实的边界。

<a id="s6-5"></a>

### 多轮对话中的图片指代

页面恢复附件不等于 Agent 已获得图片历史。现有 v1 `conversation_context` 只保存问题和
答案文字，纯图模式的空字符串不能承载“第二张图”的指代。增加严格类型的有界历史图片投影，
由 service 从同一应用、同一会话中已完成消息的 `message_images` 和最终输出模式构造：

- 最多保留最近 3 个对话轮次，图片历史数量受 [文件类型、限额与错误](07_policy.md#s11) 候选预算和下述 token 预算约束，
  按实际首次展示顺序编号。图片较多时保留最近轮次的有界前缀及总数/截断标志，
  不将“未投影”解释成“不存在”；未保留的“第 N 张”指代须澄清，不重新编号剩余图片。
  元数据包含消息 ID、展示序号、截断至 256 字符的原文图题、可用状态及来源定位。
  每张图最多投影 4 个有效来源绑定；完整绑定仍保存在数据库，投影注明是否截断。
  来源定位包含知识库、文档、generation、content version、image ID 及节点/字符窗口，
  不携带图片字节、存储路径、公开 URL、token 或应用 Key。
  指代以消息 ID 和该消息的首次展示序号定位，再解析完整来源身份，不按图题匹配；
  图题相同不合并图片。不需要携带旧 `ref_ids`，仅来源预览的附件不占答案图片序号或进入此投影。
- 投影仅用于理解指代和定位，不能自动加入本轮 collector、视为当前事实依据或图片访问授权。
  发图仍需在当前 Agent 的知识库范围内重读来源，校验完整图片身份；文档删除、范围变化或
  generation/资源变化时不得默认为“同一张图”。无法确认时澄清或说明原图不可用。
- v0/v1 的检索输入与路由均显式接收该投影；与近期文字历史使用同一批轮次裁剪。
  图片元数据额外最多占 1024 个估算 token，并计入现有总上下文预算；超预算时按整轮丢弃最旧历史，
  不截断身份字段，也不挤掉本轮问题或必需检索预算。超出保留范围的指代按信息不足处理。
- 控制台及原生 API 的服务端会话调用前，从已提交消息重新构造。
  会话仍符合现有续聊条件时，页面刷新、会话切换和服务重启后图片投影一致。
  checkpoint 中的候选选择或未提交附件不得成为图片历史；消息提交失败、取消、附件归档失败后，
  下一轮不能把这些图片说成“已经发出”。当前轮真正展示的图片以最终持久化附件为准。
- 原生 API 使用 `message + conversation_id` 续聊，服务端在现有会话准入校验后加载同一会话的图片投影。
  查询范围同时绑定应用、API 客户端、`user_id`、会话及 `channel=api`，不能只按会话或消息 ID 查询。
  未传 `conversation_id` 的新会话没有图片历史，不能按同一用户或应用自动关联其他会话。
  沿用会话固定的发布版本和现有工具契约；图片开关关闭时不得因历史中有图而开放选图工具。
  会话过期、中断、跨归属或 checkpoint 缺失仍按原有规则拒绝，不以附件记录绕过续聊校验。
- OpenAI 兼容 API 继续使用调用方提交完整 `messages` 的模式，不根据 `user` 或内部 run 自动拼接其他历史。
  调用方历史中的图题、编号和 Markdown 只作不可信指代线索，不接受其中 URL 或图片 ID 为授权。
  若已有文本历史不足以定位来源，要求补充文档或图题后重新检索，不抓取历史图片链接。

## 工具版本与历史发布兼容

沿用现有 Pydantic、TypedDict、repository 和 CSS 方案，不引入新的状态管理框架。
`select_answer_images` 改变工具集合与检索行为，需要将当前 `AGENT_TOOL_SCHEMA_VERSION`
从 4 推进到 5，相关提示词与 Agent 测试同步更新。

历史发布不能因为程序升级而失效：运行时显式支持 v4/v5 两种工具契约，根据发布版本构造工具集。
v4 保留原有工具和行为，不注入选择工具；v5 开启显式发图，控制台新运行使用 v5。
v4 不增加正文标记协议或自动附图，保持原有文字响应；请求图片能力时受理前明确返回版本不支持，
引导创建并发布 v5，不能接受后悄悄忽略开关。创建并发布 v5 后才支持原位插图与纯图流程。
新增兼容分支前不得先切换版本常量使旧发布不可运行。
发布兼容校验、运行时缓存键和历史版本测试必须同时覆盖工具契约版本。

<a id="release-compatibility"></a>

### 版本化 prompt 与运行时装配

`prompt_registry` 以发布记录已有的 `(tool_schema_version, prompt_version, v1_prompt_hash, checkpoint_schema_version)`
解析固定契约描述；描述包含主/检索/回答 prompt 构造器、固定模板、工具集合及 checkpoint state 解码规则。v4 资源随新代码保留，
支持升级前仍可运行的已知组合；v5 使用独立资源。未知哈希或依赖缺失继续 409，不任意接受历史哈希。
校验不再与当前活动 prompt 文件做唯一相等比较；也不能只通过旧哈希校验却执行新的 prompt。
`draft_config` 从当前描述生成新发布，旧配置无需增加必填字段或改写已保存的发布记录。

首版 v5 的 state 只增加可选字段，沿用 `checkpoint_schema_version=1`，旧 checkpoint 可以由对应 v4 bundle
继续解码；不能把“字段可选”误写成任意 state 兼容。若后续必须改变序列化或恢复语义，须提升 checkpoint schema，
为新版本增加 registry 组合和迁移/拒绝规则，并在发布前完成旧 checkpoint 的演练，不能只修改全局常量。

| 场景 | 契约与行为 |
| --- | --- |
| 已有已知 v4 API/WeCom 发布 | 使用固定 v4 prompt 与工具；文字可用，显式启用图片的 API 请求受理前 409 |
| 新 v5 API/OpenAI 发布 | 使用 v5 prompt；图片能力由本次 opt-in 决定，关闭时仍返回文字 |
| 新 v5 WeCom 发布 | 使用 v5 文字分支，图片能力强制关闭 |
| 已有控制台会话 | 迁移保存 v4 契约及对应 bundle 标识，继续旧 checkpoint；不因部署静默换工具 |
| 新控制台会话 | 保存当前 v5 契约，默认允许图片；图片能力不写入跨请求共享可变状态 |

兼容矩阵的四个版本字段必须整体匹配；其中首版 v4/v5 均为 `checkpoint_schema_version=1`。

控制台迁移不清理或重写 checkpoint；旧会话启用图片须新建 v5 会话。找不到受支持的会话契约时明确拒绝并提示新建，
不能猜一个当前版本继续运行。API/WeCom 已有会话仍以其固定 release 为契约来源。
契约描述和图片能力由 `ApplicationService.release_runtime/runtime` 显式传到 factory、`build_tools`、
prompt 构造与请求 context；缓存键同时包括已解析契约、bundle 标识与能力，不能只放全局版本常量。

| 调用链 | 必须传递的内容 |
| --- | --- |
| 控制台/API/OpenAI/WeCom 准入 → ApplicationService | 会话或发布的契约描述、本次渠道允许的图片能力 |
| ApplicationService → factory → tools/middleware | 固定 prompt、工具版本、能力；v0/v1 使用同一规则 |
| tools → collector → runner | 源坐标、稳定 ref、冻结批次及有界选择，不传 URL 或图片字节 |
| runner → 完成 service → repository | 规范答案、最终模式及候选；归档完成后形成唯一完成结果，公开投影独立保存 |

## 模块验收

扩展 tests/agent/test_retrieval_answer.py、tests/agent/test_tools_runtime.py 和对应主/子 Agent 测试；覆盖并行顺序、历史裁剪、纯图空正文、v4/v5 兼容与请求隔离。

具体边界场景见 [完整验收清单](08_validation.md)。
