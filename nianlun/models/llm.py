"""LLM 工厂与 content 归一化的统一入口，检索侧与索引侧共用。

被 ``agent/lead_agent`` 与 ``indexing/tree`` 共同依赖：ChatOpenAI 构造见
:func:`build_chat_model`，消息 / 裸 content 的文本提取见 :func:`content_to_text`。

env 解析复用顶层 ``nianlun.config`` 的 ``get_openai_*`` 取值函数（``.env`` 加载
也在 config 完成），本模块不自行 ``load_dotenv`` / 读 ``os.environ``。
"""

from __future__ import annotations

from nianlun.config import (
    get_openai_api_key,
    get_openai_base_url,
    get_openai_model,
)

from langchain_openai import ChatOpenAI


def build_chat_model(
    model: str | None = None,
    *,
    temperature: float = 0.0,
    enable_thinking: bool = False,
    api_key: str | None = None,
    base_url: str | None = None,
    allow_env_fallback: bool = True,
) -> ChatOpenAI:
    """构建 ChatOpenAI。

    ``allow_env_fallback`` 默认开启以保持 CLI 和独立索引管线兼容。API Server
    应显式关闭它，确保模型管理目录是唯一配置来源。

    - 默认 ``temperature=0.0``、``enable_thinking=False``（索引侧：可复现、不思考）。
    - 检索侧调用时传 ``temperature=get_openai_temperature()``（默认 0.8）、
      ``enable_thinking=get_enable_thinking()``（默认开）以保留其行为。
    - ``base_url`` 存在且 ``enable_thinking=False`` 时注入
      ``extra_body={"chat_template_kwargs": {"enable_thinking": False}}``：
      思考输出会占 output 预算且曾致 content 为空（输出全进推理通道）。
    """
    if allow_env_fallback:
        api_key = api_key or get_openai_api_key()
    if not api_key:
        raise RuntimeError("未设置 OPENAI_API_KEY")
    if allow_env_fallback:
        base_url = base_url or get_openai_base_url()
        model = model or get_openai_model()
    if not model:
        raise RuntimeError("未配置模型名称")
    kwargs: dict = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "timeout": 300,
        # 让流式响应也回带 usage（OpenAI 的 stream_options.include_usage）。非流式
        # ``invoke`` 不受影响；流式路径据此读取 token 用量与缓存命中。
        "stream_usage": True,
    }
    if base_url:
        kwargs["base_url"] = base_url
        if not enable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return ChatOpenAI(**kwargs)


# ============ content 归一化 ============


def _join_blocks(blocks) -> str:
    """把 content blocks 列表拼成纯文本。

    兼容 str / dict / 对象元素；dict 先取 ``text`` 再取 ``content``（覆盖
    ``{"type": "text", "text": ...}`` 与 ``{"content": ...}`` 两种形态）；
    对象取 ``.text`` 属性。空串块跳过，块间用 ``\\n`` 连接（保留块边界）。
    """
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            t = block.get("text")
            if not isinstance(t, str):
                t = block.get("content")
            if isinstance(t, str):
                parts.append(t)
        else:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(p for p in parts if p)


def content_to_text(resp) -> str:
    """从 LangChain 响应/消息/裸 content 提取文本。

    接受形态：
    - ``None`` / ``str``；
    - ``list``：content blocks 列表（每个 str/dict/对象），直接 ``_join_blocks``；
    - 对象 message：优先 ``.content_blocks``，再 ``.content``（str 或 list）。

    取不到文本时返回 ``""``（如思考模型 content 被吃空），**绝不返回对象
    repr 串**。
    """
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return _join_blocks(resp)
    blocks = getattr(resp, "content_blocks", None)
    if isinstance(blocks, list):
        t = _join_blocks(blocks)
        if t:
            return t
    content = getattr(resp, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        t = _join_blocks(content)
        if t:
            return t
    return ""
