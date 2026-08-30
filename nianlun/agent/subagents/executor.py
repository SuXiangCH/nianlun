"""Synchronous, isolated executor for deep-search subagents."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from nianlun.agent.subagents.config import DeepSearchConfig
from nianlun.agent.subagents.prompt import DEEP_SEARCH_SYSTEM_PROMPT
from nianlun.agent.subagents.result import (
    DeepSearchResult,
    bound_result,
    failed_result,
    result_from_agent_output,
)


class DeepSearchAgent(Protocol):
    def invoke(self, input: Any, **kwargs: Any) -> Any:
        """Invoke the child Agent without a conversation thread."""


AgentFactory = Callable[[], DeepSearchAgent]


@dataclass(frozen=True, slots=True)
class DeepSearchExecution:
    """One isolated run and its parent-facing diagnostics."""

    subagent_run_id: str
    result: DeepSearchResult
    parent_request_id: str | None = None
    status_events: tuple[dict[str, Any], ...] = ()
    config: DeepSearchConfig = field(
        default_factory=DeepSearchConfig, repr=False, compare=False
    )

    def to_dict(self, config: DeepSearchConfig | None = None) -> dict[str, Any]:
        return self.result.to_dict(config or self.config)


class DeepSearchRunner:
    """Lazily create and safely execute a stateless deep-search Agent.

    The runner intentionally accepts a factory rather than an AgentRuntime. This
    keeps the child graph independent from the parent's checkpointer, message
    history, and collector while making the module straightforward to test.
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        config: DeepSearchConfig | None = None,
        system_prompt: str = DEEP_SEARCH_SYSTEM_PROMPT,
        context_factory: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config or DeepSearchConfig()
        self._agent_factory = agent_factory
        self._system_prompt = system_prompt
        self._context_factory = context_factory
        self._agent: DeepSearchAgent | None = None
        self._agent_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(self.config.max_concurrent)

    @property
    def agent_created(self) -> bool:
        """Whether lazy child-agent compilation has happened."""
        return self._agent is not None

    def _get_agent(self) -> DeepSearchAgent:
        if self._agent is not None:
            return self._agent
        with self._agent_lock:
            if self._agent is None:
                self._agent = self._agent_factory()
        return self._agent

    def run(
        self,
        task: str,
        *,
        request_id: str | None = None,
        parent_request_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DeepSearchExecution:
        """Run one bounded child task and return a compact result envelope."""
        if request_id and parent_request_id and request_id != parent_request_id:
            raise ValueError("request_id and parent_request_id cannot disagree")
        parent_id = parent_request_id or request_id
        run_id = uuid.uuid4().hex
        events: list[dict[str, Any]] = []
        external_cancel = cancel_event or threading.Event()

        if not isinstance(task, str) or not task.strip():
            return self._execution(
                run_id,
                failed_result(
                    "invalid_task", "Deep-search task must be a non-empty string."
                ),
                events,
                parent_request_id=parent_id,
            )
        if external_cancel.is_set():
            return self._execution(
                run_id,
                failed_result(
                    "cancelled", "Deep-search task was cancelled before it started."
                ),
                events,
                parent_request_id=parent_id,
            )
        if not self._slots.acquire(blocking=False):
            return self._execution(
                run_id,
                failed_result("busy", "Too many deep-search tasks are running."),
                events,
                parent_request_id=parent_id,
            )

        started = time.monotonic()
        deadline = started + self.config.timeout_seconds

        def emit(event: str, **details: Any) -> None:
            events.append(
                self._event(event, run_id, parent_request_id=parent_id, **details)
            )

        emit("deep_search_started")
        try:
            child_context = dict(
                self._context_factory() if self._context_factory else {}
            )
            child_context.update(context or {})
        except Exception as exc:
            self._slots.release()
            result = failed_result("context_error", str(exc))
            emit("deep_search_failed", code="context_error")
            return self._execution(run_id, result, events, parent_request_id=parent_id)

        # Parent runtime configuration must never become a child thread.
        child_context.pop("thread_id", None)
        child_context.pop("configurable", None)
        child_context.update(
            {
                "subagent_run_id": run_id,
                "parent_request_id": parent_id,
                "deadline": deadline,
                "cancel_event": external_cancel,
            }
        )

        pool: ThreadPoolExecutor | None = None
        submitted = False
        try:
            pool = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="nianlun-deep-search",
            )
            future = pool.submit(self._invoke_child, task, child_context)
            submitted = True
            while True:
                if external_cancel.is_set():
                    result = failed_result(
                        "cancelled", "Deep-search task was cancelled."
                    )
                    emit("deep_search_failed", code="cancelled")
                    return self._execution(
                        run_id, result, events, parent_request_id=parent_id
                    )
                remaining = self.config.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    external_cancel.set()
                    result = failed_result("timeout", "Deep-search task timed out.")
                    emit("deep_search_failed", code="timeout")
                    return self._execution(
                        run_id, result, events, parent_request_id=parent_id
                    )
                try:
                    raw_output = future.result(timeout=min(remaining, 0.1))
                    break
                except FuturesTimeoutError:
                    continue

            if external_cancel.is_set():
                result = failed_result("cancelled", "Deep-search task was cancelled.")
                emit("deep_search_failed", code="cancelled")
                return self._execution(
                    run_id, result, events, parent_request_id=parent_id
                )

            result = bound_result(
                result_from_agent_output(raw_output, self.config), self.config
            )
            if result.status == "completed":
                emit(
                    "deep_search_completed",
                    elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
            else:
                emit(
                    "deep_search_failed",
                    code=result.error_code or "empty_result",
                )
            return self._execution(run_id, result, events, parent_request_id=parent_id)
        except Exception as exc:
            result = failed_result("model_error", str(exc))
            emit("deep_search_failed", code="model_error")
            return self._execution(run_id, result, events, parent_request_id=parent_id)
        finally:
            # The worker releases the slot in _invoke_child. shutdown(wait=False)
            # prevents a timeout from blocking the parent request indefinitely.
            if not submitted:
                self._slots.release()
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    def _invoke_child(
        self,
        task: str,
        context: dict[str, Any],
    ) -> Any:
        try:
            agent = self._get_agent()
            return agent.invoke(
                {
                    "messages": [
                        SystemMessage(content=self._system_prompt),
                        HumanMessage(content=task),
                    ]
                },
                config={
                    "recursion_limit": self.config.max_turns,
                    "timeout": max(
                        0.01,
                        float(context["deadline"]) - time.monotonic(),
                    ),
                },
                context=context,
            )
        finally:
            self._slots.release()

    @staticmethod
    def _event(
        event: str,
        run_id: str,
        *,
        parent_request_id: str | None,
        **details: Any,
    ) -> dict[str, Any]:
        payload = {"event": event, "subagent_run_id": run_id, **details}
        if parent_request_id is not None:
            payload["parent_request_id"] = parent_request_id
        return payload

    def _execution(
        self,
        run_id: str,
        result: DeepSearchResult,
        events: list[dict[str, Any]],
        *,
        parent_request_id: str | None = None,
    ) -> DeepSearchExecution:
        return DeepSearchExecution(
            subagent_run_id=run_id,
            result=result,
            parent_request_id=parent_request_id,
            status_events=tuple(events),
            config=self.config,
        )


__all__ = [
    "AgentFactory",
    "DeepSearchAgent",
    "DeepSearchExecution",
    "DeepSearchRunner",
]
