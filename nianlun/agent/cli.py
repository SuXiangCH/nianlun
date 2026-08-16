"""命令行入口：参数解析 + 交互模式 + 批量模式调度。

``main()`` 只做分发，具体逻辑落在 interactive_main / batch_main；二者各自调用
agent / runtime / batch 模块完成实际工作，避免单个函数承担过多职责。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
import uuid
from pathlib import Path
from typing import TextIO, cast

from nianlun.agent.batch import (
    default_batch_output_path,
    run_batch,
    save_batch_summary,
)
from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
from nianlun.agent.lead_agent.runtime import (
    AgentRuntime,
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="LangChain + Nianlun 多文档 Agent Demo"
    )
    parser.add_argument(
        "--batch-file",
        type=Path,
        help="批量测试问题集文件路径，支持 JSON / JSONL。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="批量测试输出文件路径，默认写入 evals/results/ 下的时间戳 JSONL。",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="从第几条记录开始跑，默认 0。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="最多跑多少条记录，默认跑到文件末尾。",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="批量测试并发 worker 数，默认 1。建议按模型限流能力调节。",
    )
    parser.add_argument(
        "--retry-times",
        type=int,
        default=3,
        help="单题失败后的重试次数，默认 3。",
    )
    parser.add_argument(
        "--quiet-tools",
        action="store_true",
        help="批量测试时关闭每次工具调用的详细日志。",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="交互模式下关闭流式输出，回退为整轮完成后一次性输出。",
    )
    return parser.parse_args()


def configure_tool_log_output() -> None:
    """为工具调用日志挂一个纯文本 handler，保持 CLI 交互可见性。

    工具 trace 走 ``logging``（默认 WARNING 级别下不可见）；交互/批量 CLI
    开启 tool_logging 时调用本函数，仅输出消息本体（不带级别/时间前缀）。
    API Server 等已配置 logging 的宿主不调用，避免重复输出。
    """
    tools_logger = logging.getLogger("nianlun.agent.tools")
    handler = next(
        (
            candidate
            for candidate in tools_logger.handlers
            if getattr(candidate, "_nianlun_cli_tool_handler", False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._nianlun_cli_tool_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(message)s"))
        tools_logger.addHandler(handler)
    else:
        # stdout may be replaced by test runners or an embedding CLI between calls.
        cast(logging.StreamHandler[TextIO], handler).setStream(sys.stdout)
    tools_logger.setLevel(logging.INFO)
    tools_logger.propagate = False


def setup(tool_logging: bool = True) -> AgentRuntime:
    """初始化运行时，失败时打印提示并退出。"""
    if tool_logging:
        configure_tool_log_output()
    try:
        runtime = AgentRuntimeFactory(tool_logging=tool_logging).create()
    except RuntimeError as exc:
        print(f"错误: {exc}")
        print("请运行: export OPENAI_API_KEY='你的API Key'")
        sys.exit(1)

    print(f"🌐 模型: {runtime.model}")
    print(f"🔗 API:  {runtime.effective_url}")

    return runtime


def interactive_main(stream: bool = True) -> None:
    runtime = setup(tool_logging=True)
    kb = runtime.kb
    last_retrieved_texts: list[str] = []
    # 多轮上下文由 agent 内置 checkpointer 按 thread_id 维护：一个交互会话用一个
    # thread_id，进程内有效（MemorySaver 不落盘，重启/新进程不延续）。
    thread_id = f"interactive-{uuid.uuid4().hex[:8]}"

    print("=" * 60)
    print("🤖 Nianlun Agent Demo (LangChain) - 多文档知识库中文版")
    print(f"📚 知识库共 {len(kb.meta)} 份文档")
    print("=" * 60)

    print("\n命令:")
    print(f"  输入问题  -> Agent 跨文档自动检索回答{'（流式输出）' if stream else ''}")
    print("  'list'   -> 列出知识库所有文档（含 doc_id）")
    print("  'trace'  -> 查看上一轮问答收集到的检索片段 text 列表")
    print("  'q'      -> 退出")
    print("（多轮对话：保留最近若干轮上下文，可追问）")
    print()

    while True:
        user_input = input("💬 ").strip()
        if not user_input or user_input.lower() in ("q", "quit", "exit"):
            break

        if user_input.lower() == "list":
            print(kb.list_documents())
            continue

        if user_input.lower() == "trace":
            print(
                json.dumps(last_retrieved_texts, ensure_ascii=False, indent=2)
                if last_retrieved_texts
                else "[]"
            )
            continue

        print("\n🤔 正在思考...\n")

        try:
            used_stream_output = False
            if stream:
                try:
                    run_result = runtime.stream_to_stdout(
                        user_input,
                        thread_id=thread_id,
                    )
                    used_stream_output = True
                except Exception as stream_exc:
                    print(f"\n⚠️ 流式输出失败，已回退到非流式模式: {stream_exc}\n")
                    run_result = runtime.invoke(
                        user_input,
                        thread_id=thread_id,
                    )
            else:
                run_result = runtime.invoke(
                    user_input,
                    thread_id=thread_id,
                )
            answer = run_result["answer"]
            # 多轮历史由 checkpointer 按 thread_id 自动维护，无需手动追加
            last_retrieved_texts = run_result["retrieved_texts"]
            route = run_result.get("route", "retrieval")
            route_source = run_result.get("route_source", "unknown")
            if not used_stream_output:
                print(f"\n{'=' * 60}")
                print(answer)
                print(f"{'=' * 60}")
            if route == "direct":
                print(
                    f"💬 本次为直接对话回复，未触发知识库检索。route_source={route_source}"
                )
            else:
                print(
                    f"🔎 本次收集到 {len(last_retrieved_texts)} 段检索文本，可输入 'trace' 查看。"
                    f" route_source={route_source}"
                )
        except Exception as exc:
            print(f"\n❌ 错误: {exc}")
            traceback.print_exc()


def batch_main(args: argparse.Namespace) -> None:
    batch_file = args.batch_file
    if batch_file is None:
        raise ValueError("batch_main 需要提供 --batch-file")

    output_path = args.output or default_batch_output_path(batch_file)
    tool_logging = not args.quiet_tools
    if tool_logging:
        configure_tool_log_output()
    runtime_factory = AgentRuntimeFactory(tool_logging=tool_logging)
    try:
        runtime = runtime_factory.create()
    except RuntimeError as exc:
        print(f"错误: {exc}")
        print("请运行: export OPENAI_API_KEY='你的API Key'")
        sys.exit(1)
    print(f"🌐 模型: {runtime.model}")
    print(f"🔗 API:  {runtime.effective_url}")

    summary = run_batch(
        runtime,
        batch_file=batch_file,
        output_path=output_path,
        offset=args.offset,
        limit=args.limit,
        workers=args.workers,
        tool_logging=tool_logging,
        retry_times=args.retry_times,
        runtime_factory=runtime_factory.create,
    )
    summary_path = save_batch_summary(output_path, summary)

    print("\n" + "=" * 60)
    print("批量测试完成")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"📄 明细: {output_path}")
    print(f"📄 汇总: {summary_path}")
    print("=" * 60)


def main() -> None:
    args = parse_args()
    if args.batch_file:
        batch_main(args)
        return
    interactive_main(stream=not args.no_stream)


if __name__ == "__main__":
    main()
