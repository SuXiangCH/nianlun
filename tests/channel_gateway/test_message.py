import hashlib
from typing import Any

import pytest

from app.channel_gateway.message import (
    NOTICE_INVALID_MESSAGE,
    NOTICE_UNSUPPORTED_MESSAGE,
    InvalidFrame,
    MessageNotice,
    normalize_message,
    sha256_text,
    truncate_reply,
)

BOT_ID = "bot-1"
CHANNEL_ID = "channel-1"
MAX_BYTES = 64 * 1024


def callback(
    *,
    msgtype: str = "text",
    text: str = "hello",
    chattype: str = "single",
    userid: str = "user-1",
    chatid: str | None = None,
    msgid: str = "msg-1",
    req_id: str | None = "req-1",
    aibotid: str = BOT_ID,
    cmd: str = "aibot_msg_callback",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "msgid": msgid,
        "aibotid": aibotid,
        "chattype": chattype,
        "from": {"userid": userid},
        "msgtype": msgtype,
        "text": {"content": text},
    }
    if chatid is not None:
        body["chatid"] = chatid
    headers: dict[str, Any] = {}
    if req_id is not None:
        headers["req_id"] = req_id
    return {"cmd": cmd, "headers": headers, "body": body}


def test_normalizes_single_chat_text():
    message = normalize_message(
        CHANNEL_ID, BOT_ID, callback(text="  hello  "), MAX_BYTES
    )
    event_id = sha256_text(f"{CHANNEL_ID}|msg-1")
    assert message.channel_id == CHANNEL_ID
    assert message.event_id == event_id
    assert message.idempotency_key == f"wcm_{event_id}"
    assert message.req_id == "req-1"
    assert message.conversation_key == "single:user-1"
    assert message.user_key == "wc_" + sha256_text(f"{CHANNEL_ID}|single:user-1")[:32]
    assert message.text == "hello"


def test_group_members_share_one_conversation():
    first = normalize_message(
        CHANNEL_ID,
        BOT_ID,
        callback(chattype="group", chatid="group-1", userid="alice"),
        MAX_BYTES,
    )
    second = normalize_message(
        CHANNEL_ID,
        BOT_ID,
        callback(chattype="group", chatid="group-1", userid="bob", msgid="msg-2"),
        MAX_BYTES,
    )
    assert first.conversation_key == "group:group-1"
    assert first.user_key == second.user_key
    assert first.event_id != second.event_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cmd": "aibot_subscribe"},
        {"aibotid": "other-bot"},
        {"msgid": ""},
        {"req_id": None},
        {"req_id": "   "},
        {"userid": ""},
        {"chattype": "external"},
        {"chattype": "group"},
        {"chattype": "group", "chatid": "   "},
    ],
)
def test_structurally_invalid_frames_are_dropped(kwargs: dict[str, Any]):
    with pytest.raises(InvalidFrame):
        normalize_message(CHANNEL_ID, BOT_ID, callback(**kwargs), MAX_BYTES)


@pytest.mark.parametrize("msgtype", ["image", "voice", "video", "file", "mixed"])
def test_non_text_messages_raise_unsupported_notice(msgtype: str):
    with pytest.raises(MessageNotice) as error:
        normalize_message(
            CHANNEL_ID, BOT_ID, callback(msgtype=msgtype, text=""), MAX_BYTES
        )
    assert error.value.code == NOTICE_UNSUPPORTED_MESSAGE


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_text_raises_invalid_notice(text: str):
    with pytest.raises(MessageNotice) as error:
        normalize_message(CHANNEL_ID, BOT_ID, callback(text=text), MAX_BYTES)
    assert error.value.code == NOTICE_INVALID_MESSAGE


def test_missing_text_payload_raises_invalid_notice():
    frame = callback()
    frame["body"].pop("text")
    with pytest.raises(MessageNotice) as error:
        normalize_message(CHANNEL_ID, BOT_ID, frame, MAX_BYTES)
    assert error.value.code == NOTICE_INVALID_MESSAGE


def test_message_limit_counts_utf8_bytes():
    exact = "a" * MAX_BYTES
    assert (
        normalize_message(CHANNEL_ID, BOT_ID, callback(text=exact), MAX_BYTES).text
        == exact
    )
    with pytest.raises(MessageNotice) as error:
        normalize_message(CHANNEL_ID, BOT_ID, callback(text=exact + "a"), MAX_BYTES)
    assert error.value.code == NOTICE_INVALID_MESSAGE
    with pytest.raises(MessageNotice):
        normalize_message(CHANNEL_ID, BOT_ID, callback(text="你好"), 5)
    assert (
        normalize_message(CHANNEL_ID, BOT_ID, callback(text="你好"), 6).text == "你好"
    )


def test_event_id_is_stable_for_non_ascii_ids():
    first = normalize_message(CHANNEL_ID, BOT_ID, callback(msgid="消息-一"), MAX_BYTES)
    second = normalize_message(CHANNEL_ID, BOT_ID, callback(msgid="消息-一"), MAX_BYTES)
    assert first.event_id == second.event_id == sha256_text(f"{CHANNEL_ID}|消息-一")


def test_sha256_text_returns_hex_digest():
    assert sha256_text("你好") == hashlib.sha256("你好".encode()).hexdigest()


def test_truncate_reply_never_splits_a_character():
    content = "你" * 10_000
    truncated = truncate_reply(content, 20 * 1024)
    assert truncated == "你" * 6826
    assert len(truncated.encode("utf-8")) == 20_478
    assert truncated.encode("utf-8").decode("utf-8") == truncated


def test_truncate_reply_keeps_content_within_limit():
    assert truncate_reply("hello", 20 * 1024) == "hello"


def test_truncate_reply_returns_empty_when_first_character_does_not_fit():
    assert truncate_reply("你", 2) == ""
    assert truncate_reply("hello", 0) == ""
