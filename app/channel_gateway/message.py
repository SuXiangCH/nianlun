"""WeCom callback validation and text normalization."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

NOTICE_UNSUPPORTED_MESSAGE = "UNSUPPORTED_MESSAGE"
NOTICE_INVALID_MESSAGE = "INVALID_MESSAGE"
NOTICE_REPLIES = {
    NOTICE_UNSUPPORTED_MESSAGE: "当前仅支持文本消息",
    NOTICE_INVALID_MESSAGE: "消息内容无效或超出限制，请检查后重试",
}


@dataclass(frozen=True)
class NormalizedMessage:
    channel_id: str
    event_id: str
    idempotency_key: str
    req_id: str
    conversation_key: str
    user_key: str
    text: str


class InvalidFrame(Exception):
    """A frame not addressed to this bot or missing required identifiers."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MessageNotice(Exception):
    """A valid frame that should be answered with a fixed user-facing notice."""

    def __init__(
        self,
        code: str,
        *,
        req_id: str = "",
        event_id: str = "",
        idempotency_key: str = "",
        conversation_key: str = "",
        user_key: str = "",
    ) -> None:
        self.code = code
        self.req_id = req_id
        self.event_id = event_id
        self.idempotency_key = idempotency_key
        self.conversation_key = conversation_key
        self.user_key = user_key
        super().__init__(code)


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def normalize_message(
    channel_id: str, bot_id: str, frame: dict[str, Any], max_bytes: int
) -> NormalizedMessage:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not isinstance(frame, dict):
        raise InvalidFrame("frame")
    if frame.get("cmd") != "aibot_msg_callback":
        raise InvalidFrame("cmd")

    headers = _mapping(frame.get("headers"))
    req_id = _identifier(headers.get("req_id")) if headers is not None else None
    if req_id is None:
        raise InvalidFrame("req_id")

    body = _mapping(frame.get("body"))
    if body is None:
        raise InvalidFrame("body")
    if body.get("aibotid") != bot_id:
        raise InvalidFrame("bot")
    msgid = _identifier(body.get("msgid"))
    if msgid is None:
        raise InvalidFrame("msgid")

    chattype = body.get("chattype")
    if chattype not in ("single", "group"):
        raise InvalidFrame("chattype")
    sender = _mapping(body.get("from"))
    userid = _identifier(sender.get("userid")) if sender is not None else None
    if userid is None:
        raise InvalidFrame("userid")

    if chattype == "group":
        chatid = _identifier(body.get("chatid"))
        if chatid is None:
            raise InvalidFrame("chatid")
        conversation_key = f"group:{chatid}"
    else:
        conversation_key = f"single:{userid}"

    event_id = sha256_text(f"{channel_id}|{msgid}")
    user_key = "wc_" + sha256_text(f"{channel_id}|{conversation_key}")[:32]

    def notice(code: str) -> MessageNotice:
        return MessageNotice(
            code,
            req_id=req_id,
            event_id=event_id,
            idempotency_key=f"wcm_{event_id}",
            conversation_key=conversation_key,
            user_key=user_key,
        )

    if body.get("msgtype") != "text":
        raise notice(NOTICE_UNSUPPORTED_MESSAGE)
    payload = _mapping(body.get("text"))
    content = payload.get("content") if payload is not None else None
    if not isinstance(content, str) or not content.strip():
        raise notice(NOTICE_INVALID_MESSAGE)
    text = content.strip()
    if len(text.encode("utf-8")) > max_bytes:
        raise notice(NOTICE_INVALID_MESSAGE)

    return NormalizedMessage(
        channel_id=channel_id,
        event_id=event_id,
        idempotency_key=f"wcm_{event_id}",
        req_id=req_id,
        conversation_key=conversation_key,
        user_key=user_key,
        text=text,
    )


def truncate_reply(content: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    size = 0
    for index, character in enumerate(content):
        size += len(character.encode("utf-8"))
        if size > max_bytes:
            return content[:index]
    return content
