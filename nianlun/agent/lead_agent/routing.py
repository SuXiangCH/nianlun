"""轻量意图路由：对明显的非知识库对话（寒暄/感谢/告别/身份/帮助）直接回复，
其余情况交给主 agent 自行决定是否检索。

规则集合与判优先级刻意集中在本文件，便于后续替换为更复杂的
路由策略时只改这里。
"""

from __future__ import annotations

import re

from nianlun.agent.lead_agent.prompt import DIRECT_HELP_TEXT


def _normalize_chat_text(text: str) -> str:
    """归一化简短对话文本，便于做轻量路由。"""
    return re.sub(r"[!！?？,，.。\s]+", "", text.strip().lower())


DIRECT_GREETINGS = {
    _normalize_chat_text(item)
    for item in ("hi", "hello", "hey", "你好", "您好", "哈喽", "嗨", "在吗", "在不在")
}
DIRECT_THANKS = {
    _normalize_chat_text(item)
    for item in ("thanks", "thank you", "thx", "谢谢", "多谢", "感谢")
}
DIRECT_BYES = {
    _normalize_chat_text(item) for item in ("bye", "goodbye", "再见", "拜拜", "回头见")
}
DIRECT_HELP_QUERIES = {
    _normalize_chat_text(item) for item in ("help", "帮助", "怎么用", "你能做什么")
}
DIRECT_IDENTITY_QUERIES = {
    _normalize_chat_text(item)
    for item in ("你是谁", "介绍一下你自己", "你是干什么的", "你是什么")
}
DIRECT_CAPABILITY_QUERIES = {
    _normalize_chat_text(item) for item in ("你能做什么", "help", "帮助", "怎么用")
}


def maybe_handle_non_retrieval_query(user_query: str) -> dict[str, str]:
    """对明显的非知识库对话直接回复，其他情况交给主 agent 自行判断。"""
    query = user_query.strip()
    if not query:
        return {
            "route": "direct",
            "answer": "请直接输入你的问题。",
            "route_source": "rule",
            "route_reason": "空输入不需要检索。",
        }

    normalized = _normalize_chat_text(query)
    lowered = query.lower()

    def build_direct_reply() -> str:
        if normalized in DIRECT_THANKS:
            return "不客气。"
        if normalized in DIRECT_BYES:
            return "再见。"
        if normalized in DIRECT_IDENTITY_QUERIES:
            return (
                "我是 Nianlun，一个多文档知识库问答助手，"
                "可以帮你检索文档、定位相关章节，并基于文档内容回答问题。"
            )
        if normalized in DIRECT_CAPABILITY_QUERIES:
            return DIRECT_HELP_TEXT
        if normalized in DIRECT_GREETINGS or (
            len(query) <= 12
            and re.fullmatch(r"(你好|您好|hello|hi|hey)[!！?？\s]*", lowered)
        ):
            return "你好。可以直接问我知识库里的文档问题。"
        return "我主要负责知识库中的文档问答。你可以直接问我文档相关问题。"

    if normalized in DIRECT_GREETINGS:
        return {
            "route": "direct",
            "answer": build_direct_reply(),
            "route_source": "rule",
            "route_reason": "命中寒暄规则。",
        }
    if normalized in DIRECT_THANKS:
        return {
            "route": "direct",
            "answer": build_direct_reply(),
            "route_source": "rule",
            "route_reason": "命中感谢规则。",
        }
    if normalized in DIRECT_BYES:
        return {
            "route": "direct",
            "answer": build_direct_reply(),
            "route_source": "rule",
            "route_reason": "命中告别规则。",
        }
    if normalized in DIRECT_IDENTITY_QUERIES:
        return {
            "route": "direct",
            "answer": build_direct_reply(),
            "route_source": "rule",
            "route_reason": "命中身份询问规则。",
        }
    if normalized in DIRECT_HELP_QUERIES:
        return {
            "route": "direct",
            "answer": build_direct_reply(),
            "route_source": "rule",
            "route_reason": "命中帮助规则。",
        }

    if len(query) <= 12 and re.fullmatch(
        r"(你好|您好|hello|hi|hey)[!！?？\s]*", lowered
    ):
        return {
            "route": "direct",
            "answer": build_direct_reply(),
            "route_source": "rule",
            "route_reason": "命中短寒暄正则规则。",
        }

    return {
        "route": "retrieval",
        "answer": "",
        "route_source": "agent",
        "route_reason": "未命中规则直答，交由主 agent 自行决定是否检索。",
    }
