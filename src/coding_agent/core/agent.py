from __future__ import annotations

import time
from typing import Any, Generator

from ..config import Config
from ..errors import AgentError, InvalidToolArgumentsError, LLMError
from ..llm.base import LLMClient
from ..llm.parser import parse_tool_arguments
from ..llm.types import ToolCallBlock, ToolMessage, UserMessage
from ..security.approval import ApprovalDecision
from ..security.modes import ExecutionMode
from ..security.permissions import PermissionManager
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

AgentStream = Generator[AgentEvent, "ApprovalDecision | None", None]


class Agent:
    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        registry: ToolRegistry,
        policy: SecurityPolicy,
        permissions: PermissionManager,
        session: Session | None = None,
    ):
        self.config = config
        self.llm = llm
        self.registry = registry
        self.policy = policy
        self.permissions = permissions
        self.session = session or Session(system_prompt=build_system_prompt(config.workspace))
        self.tool_ctx = ToolContext(
            workspace=config.workspace,
            policy=policy,
            tool_config=config.tool,
            session=self.session,
        )

    def run(self, task: str) -> AgentStream:
        self.session.add(UserMessage.of(task))
        self.session.consecutive_tool_failures = 0
        self.session.reset_repeat_tracking()
        yield TaskStarted(task=task)

        final_text = ""
        schemas = self._schemas_for_mode()

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

                text = (response.message.text or "").strip()
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

    def _schemas_for_mode(self) -> list[dict[str, Any]]:
        if self.config.execution_mode is ExecutionMode.ASK:
            return [tool.to_schema() for tool in self.registry if tool.risk is RiskLevel.READ]
        return self.registry.schemas()

    def _run_tool_calls(self, tool_calls: list[ToolCallBlock]) -> Generator[AgentEvent, Any, StopReason | None]:
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
                    self._respond_text(
                        call,
                        "用户中断了任务，该工具调用未执行。",
                        ok=False,
                        error="用户中断了任务，该工具调用未执行。",
                    )
            raise

    def _run_single_call(
        self, call: ToolCallBlock, responded: set[str]
    ) -> Generator[AgentEvent, Any, StopReason | None]:
        try:
            args = parse_tool_arguments(call.arguments, call.name)
        except InvalidToolArgumentsError as exc:
            self._respond(call, responded, ToolResult.failure(str(exc)), duration=0.0)
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

        allowed = yield from self._resolve_permission(tool, args, call, responded)
        if not allowed:
            return None

        yield ToolStarted(call_id=call.id, name=call.name, args=args)
        started = time.monotonic()
        result = self.registry.execute(call.name, args, self.tool_ctx)
        duration = time.monotonic() - started

        self._respond(call, responded, result, duration=duration)
        self.session.record_tool_outcome(ok=result.ok)
        if result.ok and result.metadata.get("diff") not in (None, "(无变化)"):
            self.session.record_progress()
        yield ToolFinished(call_id=call.id, name=call.name, result=result, duration=duration)

        return self._check_failure_threshold()

    def _resolve_permission(
        self,
        tool: Any,
        args: dict[str, Any],
        call: ToolCallBlock,
        responded: set[str],
    ) -> Generator[AgentEvent, Any, bool]:
        outcome = self.permissions.evaluate(
            tool,
            args,
            mode=self.config.execution_mode,
            always_allowed=self.session.always_allowed,
        )

        if outcome.kind == "allow":
            return True

        if outcome.kind == "deny":
            self._respond(call, responded, ToolResult.failure(outcome.deny_message or "操作被拒绝。"))
            yield Notice(f"已拒绝 {tool.name}", level="warning")
            return False

        decision = yield ApprovalRequired(request=outcome.request)
        decision = decision if isinstance(decision, ApprovalDecision) else ApprovalDecision.DENY
        if not PermissionManager.apply_decision(decision, tool.name, self.session.always_allowed):
            self._respond(
                call,
                responded,
                ToolResult.failure(PermissionManager.user_denied_message()),
            )
            yield Notice(f"已拒绝 {tool.name}", level="warning")
            return False
        return True

    def _respond(self, call: ToolCallBlock, responded: set[str], result: ToolResult, *, duration: float = 0.0) -> None:
        self._respond_text(
            call,
            result.to_model_text(),
            ok=result.ok,
            duration=duration,
            error=result.error,
        )
        responded.add(call.id)

    def _respond_text(
        self,
        call: ToolCallBlock,
        text: str,
        ok: bool,
        *,
        error: str | None = None,
        duration: float = 0.0,
    ) -> None:
        """把一次工具调用的结果写入 ``tool_history`` 事实记录，并回喂一条引用它的 ``ToolMessage``。"""
        self.session.record_tool_execution(
            call.id,
            call.name,
            call.arguments,
            ok,
            text,
            error=error,
            duration=duration,
        )
        self.session.add(ToolMessage.of(call.id))

    def _check_failure_threshold(self) -> StopReason | None:
        if self.session.consecutive_tool_failures >= self.config.max_consecutive_tool_failures:
            return StopReason.TOOL_FAILURES
        return None

    def _finish(self, reason: StopReason, final_text: str) -> TaskFinished:
        return TaskFinished(reason=reason, final_text=final_text, stats=self.session.stats())
