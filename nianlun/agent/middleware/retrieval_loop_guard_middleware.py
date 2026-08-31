"""请求级的 Agent 工具循环护栏。"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, TypedDict

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.types import Command


DEFAULT_PER_TOOL_HARD_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "search_document_nodes": 20,
        "find_semantic_documents": 4,
        "get_document": 50,
        "get_structure_outline": 50,
        "get_line_content": 200,
        "ask_clarification": 1,
    }
)

_GRAPH_STEPS_PER_MODEL_ROUND = 5
_FINALIZATION_GRAPH_STEP_MARGIN = 8
_DEFAULT_LINE_CONTENT_CHAR_LIMIT = 4_000
_MAX_LINE_CONTENT_CHAR_LIMIT = 8_000
_FINAL_TOOL_CALL_FALLBACK_ZH = "基于目前已获得的证据，我无法确认答案。"
_FINAL_TOOL_CALL_FALLBACK_EN = (
    "I cannot confirm the answer based on the evidence currently available."
)

logger = logging.getLogger(__name__)


class LoopGuardSummary(TypedDict):
    triggered: bool
    trigger: str | None
    model_rounds: int
    tool_rounds: int
    tool_calls: int
    blocked_tool_calls: int
    finalized: bool


class GuardFinalizationError(RuntimeError):
    """A failed tool-free final model call that must not restart the agent chain."""

    def __init__(self, cause: Exception, guard: LoopGuardState) -> None:
        cause_name = type(cause).__name__
        status = next(
            (
                value
                for value in (
                    getattr(cause, "status_code", None),
                    getattr(cause, "status", None),
                    getattr(getattr(cause, "response", None), "status_code", None),
                )
                if isinstance(value, int)
            ),
            None,
        )
        cause_summary = (
            cause_name if status is None else f"{cause_name} status={status}"
        )
        super().__init__(f"Guard finalization model call failed: {cause_summary}")
        self.guard = snapshot_loop_guard(guard)


@dataclass(frozen=True, slots=True)
class AgentLoopGuardConfig:
    enabled: bool = True
    repeat_warn_threshold: int = 2
    repeat_hard_limit: int = 3
    no_progress_warn_rounds: int = 2
    no_progress_hard_rounds: int = 3
    model_round_warn_threshold: int = 24
    max_model_rounds: int = 32
    tool_round_warn_threshold: int = 24
    max_tool_rounds: int = 32
    total_tool_call_warn_threshold: int = 240
    max_total_tool_calls: int = 300
    per_tool_hard_limits: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_PER_TOOL_HARD_LIMITS)
    )
    recursion_limit: int = 192
    case_timeout_seconds: float = 1_200.0

    def __post_init__(self) -> None:
        positive = (
            "repeat_warn_threshold",
            "repeat_hard_limit",
            "no_progress_warn_rounds",
            "no_progress_hard_rounds",
            "model_round_warn_threshold",
            "max_model_rounds",
            "tool_round_warn_threshold",
            "max_tool_rounds",
            "total_tool_call_warn_threshold",
            "max_total_tool_calls",
            "recursion_limit",
        )
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        for name in positive:
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.repeat_warn_threshold > self.repeat_hard_limit:
            raise ValueError("repeat_warn_threshold cannot exceed repeat_hard_limit")
        if self.no_progress_warn_rounds > self.no_progress_hard_rounds:
            raise ValueError(
                "no_progress_warn_rounds cannot exceed no_progress_hard_rounds"
            )
        if self.model_round_warn_threshold > self.max_model_rounds:
            raise ValueError(
                "model_round_warn_threshold cannot exceed max_model_rounds"
            )
        if self.tool_round_warn_threshold > self.max_tool_rounds:
            raise ValueError("tool_round_warn_threshold cannot exceed max_tool_rounds")
        if self.total_tool_call_warn_threshold > self.max_total_tool_calls:
            raise ValueError(
                "total_tool_call_warn_threshold cannot exceed max_total_tool_calls"
            )
        if self.max_tool_rounds > self.max_model_rounds:
            raise ValueError("max_tool_rounds cannot exceed max_model_rounds")
        minimum_recursion_limit = (
            self.max_model_rounds * _GRAPH_STEPS_PER_MODEL_ROUND
            + _FINALIZATION_GRAPH_STEP_MARGIN
        )
        if self.recursion_limit < minimum_recursion_limit:
            raise ValueError(
                "recursion_limit must be at least "
                f"{minimum_recursion_limit} for max_model_rounds="
                f"{self.max_model_rounds}"
            )
        if (
            isinstance(self.case_timeout_seconds, bool)
            or not isinstance(self.case_timeout_seconds, (int, float))
            or self.case_timeout_seconds <= 0
        ):
            raise ValueError("case_timeout_seconds must be positive")
        invalid = set(self.per_tool_hard_limits) - set(DEFAULT_PER_TOOL_HARD_LIMITS)
        if invalid:
            raise ValueError(f"unknown guarded tools: {sorted(invalid)}")
        if any(
            type(limit) is not int or limit <= 0
            for limit in self.per_tool_hard_limits.values()
        ):
            raise ValueError("per_tool_hard_limits must contain positive integers")
        object.__setattr__(
            self,
            "per_tool_hard_limits",
            {**DEFAULT_PER_TOOL_HARD_LIMITS, **self.per_tool_hard_limits},
        )


@dataclass(slots=True)
class LoopGuardState:
    started_at: float = field(default_factory=time.monotonic)
    model_rounds: int = 0
    tool_rounds: int = 0
    total_tool_calls: int = 0
    per_tool_calls: Counter[str] = field(default_factory=Counter)
    call_fingerprint_counts: Counter[str] = field(default_factory=Counter)
    call_set_counts: Counter[str] = field(default_factory=Counter)
    allowed_tool_call_ids: set[str] = field(default_factory=set)
    blocked_tool_call_ids: set[str] = field(default_factory=set)
    blocked_tool_call_triggers: dict[str, str] = field(default_factory=dict)
    seen_evidence_keys: set[str] = field(default_factory=set)
    consecutive_no_progress_rounds: int = 0
    current_round_has_tools: bool = False
    current_round_progress: bool = False
    pending_warnings: list[str] = field(default_factory=list)
    warned_warning_keys: set[tuple[str, str | None]] = field(default_factory=set)
    finalizing: bool = False
    final_model_started: bool = False
    finalized: bool = False
    trigger: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def snapshot_loop_guard(guard: LoopGuardState) -> LoopGuardSummary:
    """Return the bounded, content-free diagnostics exposed to callers."""
    with guard.lock:
        return {
            "triggered": guard.trigger is not None,
            "trigger": guard.trigger,
            "model_rounds": guard.model_rounds,
            "tool_rounds": guard.tool_rounds,
            "tool_calls": guard.total_tool_calls,
            "blocked_tool_calls": len(guard.blocked_tool_call_ids),
            "finalized": guard.finalized,
        }


def _field(value: Any, name: str, default: Any = None) -> Any:
    return (
        value.get(name, default)
        if isinstance(value, Mapping)
        else getattr(value, name, default)
    )


def _tool_name(call: Any) -> str:
    return str(_field(call, "name", "unknown_tool") or "unknown_tool")


def _tool_args(call: Any) -> Mapping[str, Any]:
    args = _field(call, "args", {})
    if isinstance(args, Mapping):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _tool_call_id(call: Any) -> str:
    value = _field(call, "id", "unknown-tool-call")
    return str(value) if value is not None and str(value) else "unknown-tool-call"


def _normalise(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Mapping):
        return {
            str(key): _normalise(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def tool_call_fingerprint(call: Any) -> str:
    """Return a deterministic fingerprint for a model-visible tool call."""
    name = _tool_name(call)
    args: dict[str, Any] = dict(_tool_args(call))
    raw_args = _field(call, "args", {})
    if isinstance(raw_args, str) and not args:
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed_args = None
        if not isinstance(parsed_args, Mapping):
            args = {"__raw_args__": raw_args}
    if "__raw_args__" not in args and name == "search_document_nodes":
        args = {"query": args.get("query"), "doc_ids": args.get("doc_ids")}
        if isinstance(args["doc_ids"], list):
            args["doc_ids"] = sorted(args["doc_ids"], key=str)
    elif "__raw_args__" not in args and name == "find_semantic_documents":
        args = {"query": args.get("query"), "top_k": args.get("top_k", 15)}
    elif "__raw_args__" not in args and name in {
        "get_document",
        "get_structure_outline",
    }:
        args = {"doc_id": args.get("doc_id")}
    elif "__raw_args__" not in args and name == "get_line_content":
        args = {
            "doc_id": args.get("doc_id"),
            "line_spec": args.get("line_spec"),
            "char_offset": args.get("char_offset", 0),
            "char_limit": args.get("char_limit", _DEFAULT_LINE_CONTENT_CHAR_LIMIT),
        }
        if isinstance(args["line_spec"], str):
            args["line_spec"] = "".join(args["line_spec"].split())
        char_limit = args.get("char_limit")
        if char_limit is None:
            args["char_limit"] = _DEFAULT_LINE_CONTENT_CHAR_LIMIT
        elif isinstance(char_limit, int) and char_limit > _MAX_LINE_CONTENT_CHAR_LIMIT:
            args["char_limit"] = _MAX_LINE_CONTENT_CHAR_LIMIT
    elif "__raw_args__" not in args and name == "ask_clarification":
        args = {
            "question": args.get("question"),
            "clarification_type": args.get("clarification_type"),
        }
    return json.dumps(
        {"name": name, "args": _normalise(args)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _call_set_fingerprint(calls: list[Any]) -> str:
    return json.dumps(
        sorted(tool_call_fingerprint(call) for call in calls),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _evidence_text_hash(value: Any) -> str:
    encoded = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def _state(runtime: Any) -> LoopGuardState:
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        return LoopGuardState()
    value = context.get("loop_guard_state")
    if not isinstance(value, LoopGuardState):
        value = LoopGuardState()
        context["loop_guard_state"] = value
    return value


def _message_tool_calls(state: Any) -> list[Any]:
    messages = state.get("messages", ()) if isinstance(state, Mapping) else ()
    if not messages:
        return []
    message = messages[-1]
    calls = getattr(message, "tool_calls", None)
    return list(calls) if isinstance(calls, list) else []


def _tool_result_makes_progress(
    name: str,
    args: Mapping[str, Any],
    result: ToolMessage,
    guard: LoopGuardState,
) -> bool:
    if result.status == "error" or not isinstance(result.content, str):
        return False
    if name == "get_structure_outline":
        if not result.content.strip():
            return False
        try:
            outline_payload = json.loads(result.content)
        except json.JSONDecodeError:
            outline_payload = None
        if isinstance(outline_payload, dict) and outline_payload.get("error"):
            return False
        doc_id = args.get("doc_id")
        keys = [f"get_structure_outline:{doc_id}"] if doc_id else []
        return _record_fresh_evidence_keys(keys, guard)
    try:
        payload = json.loads(result.content)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("error"):
        return False
    if name in {"search_document_nodes", "find_semantic_documents"}:
        documents = payload.get("documents")
        if not isinstance(documents, list):
            return False
        keys = []
        for document in documents:
            if not isinstance(document, dict) or not document.get("doc_id"):
                continue
            doc_id = document["doc_id"]
            node_hints = document.get("node_hints")
            node_keys = (
                [
                    f"node:{doc_id}:{hint.get('node_id')}"
                    for hint in node_hints
                    if isinstance(hint, dict) and hint.get("node_id")
                ]
                if isinstance(node_hints, list)
                else []
            )
            keys.extend(node_keys or [f"document:{doc_id}"])
    elif name == "get_document":
        doc_id = payload.get("doc_id")
        keys = [f"{name}:{doc_id}"] if doc_id else []
    elif name == "get_line_content":
        doc_id = payload.get("doc_id") or args.get("doc_id")
        content = payload.get("content")
        if not isinstance(content, list):
            return False
        keys = [
            "line:"
            f"{doc_id}:{item.get('node_id')}:{item.get('char_offset')}:"
            f"{_evidence_text_hash(item.get('text', ''))}"
            for item in content
            if isinstance(item, dict) and item.get("text")
        ]
    else:
        return False
    return _record_fresh_evidence_keys(keys, guard)


def _record_fresh_evidence_keys(keys: list[str], guard: LoopGuardState) -> bool:
    fresh = [key for key in keys if key not in guard.seen_evidence_keys]
    guard.seen_evidence_keys.update(fresh)
    return bool(fresh)


class RetrievalLoopGuardMiddleware(AgentMiddleware):
    """Stop repeated or unproductive tool loops without sharing state between requests."""

    tools = ()

    def __init__(self, config: AgentLoopGuardConfig | None = None) -> None:
        self.config = config or AgentLoopGuardConfig()
        self._per_tool_hard_limits = MappingProxyType(
            dict(self.config.per_tool_hard_limits)
        )

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if not self.config.enabled:
            return None
        guard = _state(runtime)
        calls = _message_tool_calls(state)
        with guard.lock:
            if guard.final_model_started:
                return {"jump_to": "end"}
            self._finish_previous_tool_round(guard)
            guard.model_rounds += 1
            if guard.model_rounds >= self.config.model_round_warn_threshold:
                self._queue_warning(
                    guard,
                    "model_round_limit",
                    "模型与工具已经交互较多轮，请尽快基于已有证据完成回答。",
                )
            if guard.model_rounds >= self.config.max_model_rounds:
                self._finalize(guard, "model_round_limit")
            self._finalize_if_expired(guard)
            if calls:
                guard.tool_rounds += 1
                if guard.tool_rounds >= self.config.tool_round_warn_threshold:
                    self._queue_warning(
                        guard,
                        "tool_round_limit",
                        "工具调用轮次已较多，请停止扩展检索并基于已有证据完成回答。",
                    )
                guard.current_round_has_tools = True
                guard.current_round_progress = False
                call_set_fingerprint = _call_set_fingerprint(calls)
                call_set_count = guard.call_set_counts[call_set_fingerprint] + 1
                guard.call_set_counts[call_set_fingerprint] = call_set_count
                if call_set_count >= self.config.repeat_hard_limit:
                    self._finalize(guard, "identical_call_set")
                elif call_set_count >= self.config.repeat_warn_threshold:
                    self._queue_warning(
                        guard,
                        "identical_call_set",
                        "重复的工具调用没有带来新的信息，请基于已有证据完成回答。",
                        fingerprint=call_set_fingerprint,
                    )
                if guard.tool_rounds >= self.config.max_tool_rounds:
                    self._finalize(guard, "tool_round_limit")
                self._plan_tool_calls(guard, calls)
            elif guard.finalizing:
                # This successful no-tool response already satisfies finalization.
                guard.finalized = True
            return None

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        if not self.config.enabled:
            return handler(request)
        prepared, finalizing = self._prepare_model_request(request)
        try:
            response = handler(prepared)
        except Exception as exc:
            if finalizing:
                raise GuardFinalizationError(exc, _state(request.runtime)) from None
            raise
        if finalizing:
            response = self._sanitize_final_model_response(
                response,
                fallback_text=_final_tool_call_fallback(prepared.messages),
            )
            guard = _state(request.runtime)
            with guard.lock:
                guard.finalized = True
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self.config.enabled:
            return await handler(request)
        prepared, finalizing = self._prepare_model_request(request)
        try:
            response = await handler(prepared)
        except Exception as exc:
            if finalizing:
                raise GuardFinalizationError(exc, _state(request.runtime)) from None
            raise
        if finalizing:
            response = self._sanitize_final_model_response(
                response,
                fallback_text=_final_tool_call_fallback(prepared.messages),
            )
            guard = _state(request.runtime)
            with guard.lock:
                guard.finalized = True
        return response

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        if not self.config.enabled:
            return handler(request)
        return self._handle_tool_call(request, handler)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if not self.config.enabled:
            return await handler(request)
        blocked = self._blocked_tool_message(request)
        if blocked is not None:
            return blocked
        result = await handler(request)
        self._record_tool_result(request, result)
        return result

    def _prepare_model_request(
        self, request: ModelRequest
    ) -> tuple[ModelRequest, bool]:
        guard = _state(request.runtime)
        with guard.lock:
            self._finalize_if_expired(guard)
            if guard.finalizing:
                guard.final_model_started = True
                return (
                    request.override(
                        tools=[],
                        tool_choice="none",
                        system_message=_append_system_message(
                            request.system_message,
                            "请停止调用工具，仅基于当前已获得的证据直接回答。证据不足时明确说明无法确认。",
                        ),
                    ),
                    True,
                )
            warnings = list(dict.fromkeys(guard.pending_warnings))
            guard.pending_warnings.clear()
        if warnings:
            return (
                request.override(
                    system_message=_append_system_message(
                        request.system_message, "\n".join(warnings)
                    )
                ),
                False,
            )
        return request, False

    def _handle_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        blocked = self._blocked_tool_message(request)
        if blocked is not None:
            return blocked
        result = handler(request)
        self._record_tool_result(request, result)
        return result

    def _blocked_tool_message(self, request: ToolCallRequest) -> ToolMessage | None:
        guard = _state(request.runtime)
        name = _tool_name(request.tool_call)
        call_id = _tool_call_id(request.tool_call)
        with guard.lock:
            expired = self._finalize_if_expired(guard)
            if call_id in guard.allowed_tool_call_ids and not expired:
                return None
            if expired:
                guard.allowed_tool_call_ids.discard(call_id)
                guard.blocked_tool_call_ids.add(call_id)
                guard.blocked_tool_call_triggers[call_id] = "timeout"
            if call_id not in guard.blocked_tool_call_ids:
                self._plan_tool_calls(guard, [request.tool_call])
            blocked = call_id in guard.blocked_tool_call_ids
            if blocked:
                trigger = guard.blocked_tool_call_triggers.get(
                    call_id, guard.trigger or "guard_finalizing"
                )
                return ToolMessage(
                    content=json.dumps(
                        {
                            "error": {
                                "code": _blocked_error_code(trigger),
                                "tool": name,
                                "tool_call_id": _tool_call_id(request.tool_call),
                                "retryable": False,
                                "suggested_action": "Use the evidence already collected to answer the user.",
                            }
                        },
                        ensure_ascii=False,
                    ),
                    tool_call_id=_tool_call_id(request.tool_call),
                    name=name,
                    status="error",
                )
            return None

    def _plan_tool_calls(self, guard: LoopGuardState, calls: list[Any]) -> None:
        """Reserve a model batch in stable order before parallel tool execution."""
        for call in calls:
            call_id = _tool_call_id(call)
            if (
                call_id in guard.allowed_tool_call_ids
                or call_id in guard.blocked_tool_call_ids
            ):
                continue
            name = _tool_name(call)
            fingerprint = tool_call_fingerprint(call)
            fingerprint_count = guard.call_fingerprint_counts[fingerprint] + 1
            guard.call_fingerprint_counts[fingerprint] = fingerprint_count
            limit = self._per_tool_hard_limits.get(name)
            trigger = None
            if guard.finalizing:
                trigger = guard.trigger or "guard_finalizing"
            elif limit is not None and guard.per_tool_calls[name] >= limit:
                trigger = "per_tool_limit"
            elif guard.total_tool_calls >= self.config.max_total_tool_calls:
                trigger = "total_tool_limit"
            elif fingerprint_count >= self.config.repeat_hard_limit:
                trigger = "identical_tool_call"

            if trigger is not None:
                guard.blocked_tool_call_ids.add(call_id)
                guard.blocked_tool_call_triggers[call_id] = trigger
                self._finalize(guard, trigger)
                continue

            guard.allowed_tool_call_ids.add(call_id)
            guard.total_tool_calls += 1
            guard.per_tool_calls[name] += 1
            if guard.total_tool_calls >= self.config.total_tool_call_warn_threshold:
                self._queue_warning(
                    guard,
                    "total_tool_limit",
                    "工具调用总量已较高，请停止扩展检索并基于已有证据完成回答。",
                )
            if fingerprint_count >= self.config.repeat_warn_threshold:
                self._queue_warning(
                    guard,
                    "identical_tool_call",
                    "重复的工具调用没有带来新的信息，请基于已有证据完成回答。",
                    fingerprint=fingerprint,
                )

    def _record_tool_result(
        self, request: ToolCallRequest, result: ToolMessage | Command[Any]
    ) -> None:
        if not isinstance(result, ToolMessage):
            return
        guard = _state(request.runtime)
        with guard.lock:
            guard.current_round_progress = (
                guard.current_round_progress
                or _tool_result_makes_progress(
                    _tool_name(request.tool_call),
                    _tool_args(request.tool_call),
                    result,
                    guard,
                )
            )

    def _finish_previous_tool_round(self, guard: LoopGuardState) -> None:
        if not guard.current_round_has_tools:
            return
        if guard.current_round_progress:
            guard.consecutive_no_progress_rounds = 0
        else:
            guard.consecutive_no_progress_rounds += 1
        if guard.consecutive_no_progress_rounds >= self.config.no_progress_hard_rounds:
            self._finalize(guard, "no_progress")
        elif (
            guard.consecutive_no_progress_rounds >= self.config.no_progress_warn_rounds
        ):
            self._queue_warning(
                guard,
                "no_progress",
                "最近的检索没有新增证据，请停止重复调用工具并完成回答。",
            )
        guard.current_round_has_tools = False

    @staticmethod
    def _finalize(guard: LoopGuardState, trigger: str) -> None:
        if guard.trigger is None:
            logger.warning(
                "agent.guard.finalizing trigger=%s model_rounds=%s tool_rounds=%s "
                "tool_calls=%s blocked=%s",
                trigger,
                guard.model_rounds,
                guard.tool_rounds,
                guard.total_tool_calls,
                len(guard.blocked_tool_call_ids),
            )
        guard.finalizing = True
        guard.trigger = guard.trigger or trigger

    @staticmethod
    def _queue_warning(
        guard: LoopGuardState,
        trigger: str,
        message: str,
        *,
        fingerprint: str | None = None,
    ) -> None:
        warning_key = (trigger, fingerprint)
        if warning_key in guard.warned_warning_keys:
            return
        guard.warned_warning_keys.add(warning_key)
        if message not in guard.pending_warnings:
            guard.pending_warnings.append(message)
        logger.warning(
            "agent.guard.warning trigger=%s model_rounds=%s tool_rounds=%s "
            "tool_calls=%s",
            trigger,
            guard.model_rounds,
            guard.tool_rounds,
            guard.total_tool_calls,
        )

    @staticmethod
    def _sanitize_final_model_response(
        response: ModelResponse, *, fallback_text: str
    ) -> ModelResponse:
        sanitized_messages = []
        changed = False
        for message in response.result:
            if not isinstance(message, AIMessage):
                sanitized_messages.append(message)
                continue

            additional_kwargs = dict(message.additional_kwargs)
            had_raw_tool_calls = any(
                key in additional_kwargs for key in ("tool_calls", "function_call")
            )
            additional_kwargs.pop("tool_calls", None)
            additional_kwargs.pop("function_call", None)

            response_metadata = dict(message.response_metadata)
            tool_finish_reason = response_metadata.get("finish_reason") in {
                "tool_calls",
                "function_call",
            }
            tool_stop_reason = response_metadata.get("stop_reason") == "tool_use"
            if tool_finish_reason:
                response_metadata["finish_reason"] = "stop"
            if tool_stop_reason:
                response_metadata["stop_reason"] = "end_turn"

            requested_tool = bool(
                message.tool_calls
                or message.invalid_tool_calls
                or had_raw_tool_calls
                or tool_finish_reason
                or tool_stop_reason
            )
            if requested_tool:
                message = message.model_copy(
                    update={
                        "content": fallback_text,
                        "tool_calls": [],
                        "invalid_tool_calls": [],
                        "additional_kwargs": additional_kwargs,
                        "response_metadata": response_metadata,
                    }
                )
                changed = True
            sanitized_messages.append(message)

        if not changed:
            return response
        return replace(response, result=sanitized_messages)

    def _finalize_if_expired(self, guard: LoopGuardState) -> bool:
        if time.monotonic() - guard.started_at < self.config.case_timeout_seconds:
            return False
        self._finalize(guard, "timeout")
        return True


def _final_tool_call_fallback(messages: list[Any]) -> str:
    """Choose a safe fallback for the current request's primary languages."""
    for message in reversed(messages):
        message_type = _field(message, "type", _field(message, "role", ""))
        if message_type not in {"human", "user"}:
            continue
        content = _field(message, "content", "")
        text = content if isinstance(content, str) else str(content)
        if any("\u4e00" <= char <= "\u9fff" for char in text):
            return _FINAL_TOOL_CALL_FALLBACK_ZH
        if text.strip():
            return _FINAL_TOOL_CALL_FALLBACK_EN
    return _FINAL_TOOL_CALL_FALLBACK_ZH


def _append_system_message(
    existing: SystemMessage | None, addition: str
) -> SystemMessage:
    prefix = existing.text if existing is not None else ""
    return SystemMessage(content=f"{prefix}\n\n{addition}".strip())


def _blocked_error_code(trigger: str) -> str:
    if trigger in {
        "per_tool_limit",
        "total_tool_limit",
        "model_round_limit",
        "tool_round_limit",
    }:
        return "budget_exhausted"
    if trigger == "timeout":
        return "timeout"
    if trigger in {
        "identical_call_set",
        "identical_tool_call",
        "no_progress",
    }:
        return "loop_detected"
    return "tool_call_blocked"


__all__ = [
    "AgentLoopGuardConfig",
    "DEFAULT_PER_TOOL_HARD_LIMITS",
    "GuardFinalizationError",
    "LoopGuardState",
    "LoopGuardSummary",
    "RetrievalLoopGuardMiddleware",
    "snapshot_loop_guard",
    "tool_call_fingerprint",
]
