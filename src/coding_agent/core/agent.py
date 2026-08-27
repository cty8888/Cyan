"""Agent Loop —— 整个系统的核心。

``run()`` 是一个 generator：向外 yield 事件，通过 ``send()`` 接收审批决策。
这样内核完全不碰输入输出，既方便替换前端，也方便在测试中驱动。

调用方式：

    gen = agent.run("把 foo.py 的 bug 修掉")
    reply = None
    while True:
        try:
            event = gen.send(reply)
        except StopIteration:
            break
        reply = None
        if isinstance(event, ApprovalRequired):
            reply = ask_user(event.request)
"""

from __future__ import annotations

import time
from typing import Any, Generator

from ..config import Config
from ..errors import AgentError, InvalidToolArgumentsError, LLMError
from ..llm.base import LLMClient
from ..llm.parser import parse_tool_arguments
from ..llm.types import Message, ToolCall
from ..security.approval import ApprovalDecision
from ..security.policy import SecurityPolicy
from ..tools.base import RiskLevel, ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from .events import (
    AgentEvent,
    ApprovalRequired,
    AssistantMessage,
    Notice,
    StopReason,
    TaskFinished,
    TaskStarted,
    Thinking,
    ToolFinished,
    ToolStarted,
)
from .prompts import build_system_prompt
from .session import Session

# generator 既 yield 事件，也接收审批决策
AgentStream = Generator[AgentEvent, "ApprovalDecision | None", None]


class Agent:
    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        registry: ToolRegistry,
        policy: SecurityPolicy,
        session: Session | None = None,
    ):
        self.config = config
        self.llm = llm
        self.registry = registry
        self.policy = policy
        self.session = session or Session(system_prompt=build_system_prompt(config.workspace))
        self.tool_ctx = ToolContext(
            workspace=config.workspace, policy=policy, config=config, session=self.session
        )

    # ------------------------------------------------------------------ 主循环
    def run(self, task: str) -> AgentStream:
        self.session.add(Message.user(task))
        self.session.consecutive_tool_failures = 0
        self.session.reset_repeat_tracking()
        yield TaskStarted(task=task)

        final_text = ""
        schemas = self.registry.schemas()

        try:
            for iteration in range(1, self.config.max_iterations + 1):
                yield Thinking(iteration=iteration)

                try:
                    response = self.llm.chat(self.session.messages_for_request(), tools=schemas)
                except LLMError as exc:
                    yield Notice(f"模型调用失败：{exc}", level="error")
                    yield self._finish(StopReason.FATAL_ERROR, final_text)
                    return

                self.session.record_usage(response.usage)
                self.session.add(response.message)

                text = (response.message.content or "").strip()
                if text:
                    final_text = text
                    yield AssistantMessage(text=text)

                tool_calls = response.message.tool_calls
                if not tool_calls:
                    yield self._finish(StopReason.COMPLETED, final_text)
                    return

                stop_reason = yield from self._run_tool_calls(tool_calls)
                if stop_reason is not None:
                    yield self._finish(stop_reason, final_text)
                    return

            yield Notice(
                f"已达到最大轮次上限（{self.config.max_iterations}），任务可能尚未完成。",
                level="warning",
            )
            yield self._finish(StopReason.MAX_ITERATIONS, final_text)

        except KeyboardInterrupt:
            yield self._finish(StopReason.USER_ABORT, final_text)
        except AgentError as exc:
            yield Notice(f"发生错误：{exc}", level="error")
            yield self._finish(StopReason.FATAL_ERROR, final_text)

    # -------------------------------------------------------------- 工具调用批次
    def _run_tool_calls(self, tool_calls: list[ToolCall]) -> Generator[AgentEvent, Any, StopReason | None]:
        """执行一批工具调用。返回非 None 表示应当终止循环。

        每个 tool_call 都必须回一条 tool 消息，否则下一轮请求会因缺少响应而被服务端拒绝，
        因此这里用 ``responded`` 兜底，中断时补齐占位响应。
        """
        responded: set[str] = set()
        try:
            for call in tool_calls:
                stop_reason = yield from self._run_single_call(call, responded)
                if stop_reason is not None:
                    return stop_reason
            return None
        except KeyboardInterrupt:
            for call in tool_calls:
                if call.id not in responded:
                    self.session.add(Message.tool(call.id, "用户中断了任务，该工具调用未执行。"))
            raise

    def _run_single_call(
        self, call: ToolCall, responded: set[str]
    ) -> Generator[AgentEvent, Any, StopReason | None]:
        # 1. 解析参数：失败也要回喂，让模型有机会自己改正
        try:
            args = parse_tool_arguments(call.arguments, call.name)
        except InvalidToolArgumentsError as exc:
            self._respond(call, responded, ToolResult.failure(str(exc)))
            self.session.record_tool_outcome(ok=False)
            yield Notice(f"{call.name} 参数解析失败：{exc}", level="warning")
            return self._check_failure_threshold()

        if not self.registry.has(call.name):
            result = ToolResult.failure(
                f"不存在名为 {call.name} 的工具，可用工具：{', '.join(t.name for t in self.registry)}"
            )
            self._respond(call, responded, result)
            self.session.record_tool_outcome(ok=False)
            return self._check_failure_threshold()

        # 2. 重复调用检测：模型卡在同一动作上时及时打断
        repeats = self.session.record_call_fingerprint(call.name, args)
        if repeats >= self.config.max_repeated_calls:
            self._respond(
                call,
                responded,
                ToolResult.failure(
                    f"最近若干次调用中，完全相同的 {call.name} 调用已出现 {repeats} 次，"
                    "期间没有任何实质进展，已阻止以避免死循环。"
                ),
            )
            yield Notice(f"重复调用 {call.name} 且无进展，已终止任务。", level="error")
            return StopReason.REPEATED_CALLS
        if repeats == self.config.max_repeated_calls - 1:
            yield Notice(f"{call.name} 已被重复调用 {repeats} 次，请换一种方式。", level="warning")

        tool = self.registry.get(call.name)

        # 3. 分级审批
        decision = yield from self._request_approval(tool, args)
        if decision is ApprovalDecision.DENY:
            self._respond(
                call,
                responded,
                ToolResult.failure("用户拒绝了此操作。请不要重试，改用其他方案或询问用户的意见。"),
            )
            yield Notice(f"已拒绝 {call.name}", level="warning")
            return None
        if decision is ApprovalDecision.ALLOW_ALWAYS:
            self.session.always_allowed.add(tool.name)

        # 4. 执行
        yield ToolStarted(call_id=call.id, name=call.name, args=args)
        started = time.monotonic()
        result = self.registry.execute(call.name, args, self.tool_ctx)
        duration = time.monotonic() - started

        self._respond(call, responded, result)
        self.session.record_tool_outcome(ok=result.ok)
        # 文件真的被改动了就算实质进展，重复调用窗口可以清零
        if result.ok and result.metadata.get("diff") not in (None, "(无变化)"):
            self.session.record_progress()
        yield ToolFinished(call_id=call.id, name=call.name, result=result, duration=duration)

        return self._check_failure_threshold()

    # ------------------------------------------------------------------ 辅助
    def _request_approval(self, tool: Any, args: dict[str, Any]) -> Generator[AgentEvent, Any, ApprovalDecision]:
        """按策略决定是否需要询问用户。"""
        if tool.risk is RiskLevel.READ:
            return ApprovalDecision.ALLOW_ONCE

        request = self.policy.build_approval(tool, args)
        if request is None:
            return ApprovalDecision.ALLOW_ONCE

        # 敏感操作（force）不受 --yolo 与「始终允许」影响
        if not request.force:
            if self.config.yolo or tool.name in self.session.always_allowed:
                return ApprovalDecision.ALLOW_ONCE

        decision = yield ApprovalRequired(request=request)
        # 外部没有回传决策时，按最保守的方式处理
        return decision if isinstance(decision, ApprovalDecision) else ApprovalDecision.DENY

    def _respond(self, call: ToolCall, responded: set[str], result: ToolResult) -> None:
        self.session.add(Message.tool(call.id, result.to_model_text()))
        responded.add(call.id)

    def _check_failure_threshold(self) -> StopReason | None:
        if self.session.consecutive_tool_failures >= self.config.max_consecutive_tool_failures:
            return StopReason.TOOL_FAILURES
        return None

    def _finish(self, reason: StopReason, final_text: str) -> TaskFinished:
        return TaskFinished(reason=reason, final_text=final_text, stats=self.session.stats())
