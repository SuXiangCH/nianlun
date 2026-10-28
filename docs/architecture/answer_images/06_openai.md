# OpenAI 兼容输出与公开流式

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md)

## 职责与依赖

输出标准 Markdown，保证公开流式的图文顺序、授权时点和阻塞响应一致。

使用 [发布 API](05_public_api.md) 的统一结果加载器；URL 模式读取保存的 answer/grant，
Data URL 模式按描述重建，不再次选图。原生公开 SSE 复用本模块的投影与缓冲规则。

## 计划改动位置

以下是设计约定的改动位置。多个模块共用契约和实现，不建立重复流程。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 修改 | `app/api_server/apis/v1/runtime/openai.py` | 图片/transport 请求头、标准 content、Location 与重放。 |
| 修改 | `app/api_server/apis/v1/runtime/api.py` | 原生公开 SSE 复用标准投影，保留原 envelope 与事件。 |
| 修改 | `app/api_server/services/application_invocation_service.py` | 图片选项纳入指纹，公开 worker 队列持续消费和结果协调。 |
| 新增 | `app/api_server/services/answer_image_projection.py` | 与原生 API 共用 AST 投影、安全前缀及有界缓冲。 |
| 修改 | `app/api_server/main.py` | CORS 暴露 Location 响应头。 |
| 修改 | `docs/application_api.md` | SDK 获取 Location、查询 run、同源校验与图片刷新。 |

<a id="s10"></a>

## OpenAI 兼容输出

保持 `choices[].message.content` 为字符串，不新增自定义图片 content block。
拟增加可选请求头 `X-Nianlun-Include-Images: true`，未传时维持纯文本行为。
新增 `X-Nianlun-Image-Transport: url|data_url`（默认 url），与图片开关共同校验并进入幂等指纹。
该输出选项纳入幂等指纹；SDK 可使用现有 `extra_headers` 传递。

开启后，适配层与原生 API 共用 [原生 API](05_public_api.md#s9-1) 的展示投影，在原图片位置输出标准 Markdown：

```markdown
系统包含三个主要模块。[2]

![图 3 系统架构](https://api.example.test/api/v1/runtime/images/9b03ec91-3436-4376-9d40-d49619937407?token=EXAMPLE)
```

URL 仅由服务端保存的 grant 构造；图题经过 Markdown 转义，不允许注入新链接或 HTML。
投影保留既有正文 citation，不额外插入“原图来源”行；控制台图题/来源由附件组件呈现，
公开与内部正文可有不同字节表示，但图片位置、有效来源和顺序一致。
数据库中的内部规范答案保留逻辑标记，公开 run 结果保存完整标准 Markdown 投影。
OpenAI 阻塞、流式和重放使用统一加载器产生的公开 `result.answer`，不能再额外附加图片清单到末尾。
不可用位置改为同语言说明；URL 模式重复资源的后续位置使用同图普通链接，
Data URL 模式使用“见前图”文字及来源编号，避免重复携带 Base64。
公开图片 ID、ref_id 与 grant 对应关系固定，刷新只更新调用方的展示链接，不改原始幂等结果。

<a id="s10-1"></a>

### Data URL 兼容模式

选择 `data_url` 时，同一位置输出 `![可信图题](data:image/png;base64,...)`，仍使用字符串 content，
不新增多模态 content block，不要求客户端访问图片服务。不承诺所有 OpenAI 客户端/Markdown 渲染器都允许 data scheme；
调用方需自行确认渲染器和 CSP 支持。数据前缀及编码只由可信服务端投影产生，模型输出的任意 data URL 不因此获得信任。
详情、SDK 及幂等重放遵守 [09](09_transport.md) 的预算/重授权规则，不把 Data URL 写入 result_json。
首版不因选择此输出模式向模型发送图片；未来视觉输入编码独立设计和启用。

### 公开流式与结果恢复

流式采用“安全文字前缀 + 持久化后的剩余图文”策略：

1. 图片能力关闭时保留现有文本转发。开启时使用与 [流式行为](04_console.md#s8-3) 同语法边界的行级增量解析器，
   只提前发送已确认不会重写的文字；代码、引用式图片、HTML 和未闭合标记必须按最终 AST
   的相同规则处理。候选行或未确定语法不直接输出，避免泄漏内部标记和外部图片地址。
2. 遇到第一个图片节点或需要校验/转换的图片语法后，暂存它及所有后续正文。
   不能先发后面的文字，再把图片补到末尾；模型及执行 worker 的 token 队列需持续消费，
   规范文本缓冲位于适配层并受 [文件类型、限额与错误](07_policy.md#s11) 字节预算限制，不能因暂停发送而堵住 worker 的完成事务。
   Data URL 的编码输出另受内联预算限制，不能误用规范文本 1 MiB 缓冲上限；提交后按 09 的分块大小输出。
3. 完成归档、权限校验、URL 模式 grant 及 run 事务后，用统一加载器的完整投影检查已发送内容为其前缀，
   再按字节顺序发送剩余 content delta，最后发送 finish chunk、可选 usage 和 `[DONE]`。
   心跳在暂存期间仍继续；首图后的文字可能延后，这是保留原位插图与完成后授权的明确取舍。
4. 纯图成功时从完整投影输出全部原图；图片全部失败则输出已保存的说明一次。
   没有模型正文 token 但最终答案非空时也补发；不再调用模型生成降级说明。
5. URL 幂等重放直接输出保存的 answer 一次，不重签 grant；Data URL 重放按 09 重新授权，
   在资源有效时从固定模板重建 answer，撤销后不返回原图，不追加新图片或执行模型。
   若前缀不一致，返回协议错误且不成功结束；提交失败、取消或未知异常同样不输出暂存图文和成功结束标记。

首版所有启用图片的原生流式响应也走这个标准 Markdown 投影路径，事件名称和 envelope 不变；
控制台的规范标记流不直接用于公网。首图之前的文字须经增量与最终解析一致性测试，
无法确认边界时允许多缓冲，不能以减少等待为由提前输出之后需改写的内容。

不承诺所有 OpenAI 客户端都会展示 Markdown 图片；不支持图片的客户端仍可读取文字和链接。
普通完成响应不增加 run_id 等非标准字段。第二阶段必须为阻塞和流式成功响应都设置
`Location`，首次执行和幂等重放都保持同一查询地址；OpenAI 阻塞分支通过 FastAPI Response
写入响应头，再返回标准完成 JSON。原生和 OpenAI 的阻塞、流式与 run 查询均返回 `private, no-store`。
浏览器跨域部署时在 CORS 中暴露 Location，不要求客户端从 `chatcmpl-` ID 字符串推导 run ID。
现有 CORS `allow_headers=["*"]` 已允许 `X-Nianlun-Include-Images` 及 `X-Nianlun-Image-Transport`，保持该能力；
若将来收紧请求头白名单，须保留这两个头及既有鉴权、内容类型和幂等请求头，并验证预检。

Location 指向已有的 run 查询接口并包含 user_id；接入方通过有效应用 Key 读取原生结果中的
`run_id`、`images[].id`、`images[].url` 和 `images[].expires_at`。
客户端刷新时提交原调用的 user_id；未传 OpenAI `user` 时使用运行时固定值 `openai`。
刷新接口使用现有 JSON envelope 返回 `{id, url, expires_at}`，由接入方更新本地图片地址。
Location 即使是相对地址，也应相对于调用所使用的公开 API 服务根地址解析。
所有构造出的地址必须属于配置的公开 API 根地址，SDK 示例不向任意响应头地址转发应用 Key。

拟议 SDK 接入示例；`result.answer` 为标准 Markdown，结构化展示与链接刷新使用 images 清单：
该示例为默认 URL 模式；内联调用通过 extra_headers 增加 transport=data_url，附件 expires_at 为 null，不调用刷新接口。

```python
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx
from openai import OpenAI

api_root = os.environ["NIANLUN_BASE"].rstrip("/")
app_id = os.environ["NIANLUN_APP_ID"]
app_key = os.environ["NIANLUN_APP_KEY"]
user_id = "employee-123"
client = OpenAI(base_url=f"{api_root}/v1", api_key=app_key)

raw = client.chat.completions.with_raw_response.create(
    model=app_id,
    user=user_id,
    messages=[{"role": "user", "content": "请介绍系统架构"}],
    extra_headers={"X-Nianlun-Include-Images": "true"},
)
completion = raw.parse()
print(completion.choices[0].message.content)

run_url = urljoin(f"{api_root}/", raw.headers["Location"])
expected = urlsplit(api_root)
actual = urlsplit(run_url)
if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
    raise ValueError("Unexpected run result origin")

with httpx.Client(headers={"Authorization": f"Bearer {app_key}"}) as http:
    response = http.get(run_url)
    response.raise_for_status()
    run = response.json()["data"]
    result = run["result"]
    # 仅在链接需要刷新时调用；不重新执行模型请求。
    for image in result["images"]:
        if not image["url"] or not image["expires_at"]:
            continue
        expires_at = datetime.fromisoformat(image["expires_at"])
        if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            continue
        response = http.post(
            f"{api_root}/api/v1/runtime/apps/{app_id}/runs/"
            f"{run['run_id']}/images/{image['id']}/access",
            json={"user_id": user_id},
        )
        response.raise_for_status()
        refreshed = response.json()["data"]
        # 将 refreshed["url"] 和 refreshed["expires_at"] 更新到接入方的展示状态。
```

流式 SDK 同样通过 `with_streaming_response.create(stream=True, ...)` 获取 Location 响应头，
在响应上下文内读取 SSE；使用普通 SDK 流接口而未读取响应头的接入方不能假设正文内含刷新信息。
调用方继续提交 assistant 历史时，服务端不抓取其中图片链接，图片请求输入仍不在本阶段开放。

## 模块验收

扩展 tests/api_server/test_public_runtime.py 和跨端 API 集成测试；覆盖任意字符分块、首图后全文缓冲、心跳/取消、失败说明一次，以及首次/重放、阻塞/流式一致。

具体边界场景见 [完整验收清单](08_validation.md)。
