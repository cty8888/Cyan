"""Agent Loop：驱动 LLM ↔ 工具 循环，通过事件流与 CLI 交互。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generator

from ..errors import AgentError, InvalidToolArgumentsError, LLMError
from ..llm.parser import parse_tool_arguments
from ..llm.types import ToolCallBlock, ToolMessage, UserMessage
from ..security.messages import USER_DENIED_MSG
from ..security.types import ApprovalDecision
from ..session import WorkspaceAccess
from ..tools.types import ToolContext, ToolRunResult
from .types import (
    AgentEvent,
    AgentStream,
    ApprovalRequired,
    AssistantReply,
    Notice,
    StopReason,
    TaskFinished,
    TaskStarted,
    Thinking,
    ToolFinished,
    ToolStarted,
)

if TYPE_CHECKING:
    from ..session import Session
    from ..settings import AgentSettings
    from .runtime import Runtime


@dataclass
class AgentLoop:
    """单次任务的事件驱动循环，由 Runtime 持有并驱动。"""

    runtime: Runtime

    @property
    def session(self) -> Session:
        return self.runtime.session

    @property
    def settings(self) -> AgentSettings:
        return self.runtime.settings

    @property
    def tool_ctx(self) -> ToolContext:
        return ToolContext(
            workspace=self.settings.workspace,
            tool_limits=self.settings.tools,
            workspace_access=WorkspaceAccess(self.session),
        )

    def run(self, task: str) -> AgentStream:
        self.session.state.current_task = task
        self.session.add(UserMessage.of(task))
        self.session.consecutive_tool_failures = 0
        self.session.reset_repeat_tracking()
        yield TaskStarted(task=task)

        final_text = ""
        schemas = self.runtime.schemas_for_mode()

        try:
            for iteration in range(1, self.settings.loop.max_iterations + 1):
                yield Thinking(iteration=iteration)

                try:
                    response = self.runtime.call_llm(self.runtime.messages_for_request(), tools=schemas)
                except LLMError as exc:
                    yield Notice(f"模型调用失败：{exc}", level="error")
                    yield self._finish(StopReason.FATAL_ERROR, final_text)
                    return

                self.session.record_usage(response.usage)
                self.session.add(response.message)

                text = (response.message.text or "").strip()
                if text:
                    final_text = text
                    yield AssistantReply(text=text)

                tool_calls = response.message.tool_calls
                if not tool_calls:
                    yield self._finish(StopReason.COMPLETED, final_text)
                    return

                stop_reason = yield from self._run_tool_calls(tool_calls)
                if stop_reason is not None:
                    yield self._finish(stop_reason, final_text)
                    return

            yield Notice(
                f"已达到最大轮次上限（{self.settings.loop.max_iterations}），任务可能尚未完成。",
                level="warning",
            )
            yield self._finish(StopReason.MAX_ITERATIONS, final_text)

        except KeyboardInterrupt:
            yield self._finish(StopReason.USER_ABORT, final_text)
        except AgentError as exc:
            yield Notice(f"发生错误：{exc}", level="error")
            yield self._finish(StopReason.FATAL_ERROR, final_text)

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
            self._respond(call, responded, ToolRunResult.failure(str(exc)), duration=0.0)
            yield Notice(f"{call.name} 参数解析失败：{exc}", level="warning")
            return self._check_failure_threshold()

        if not self.runtime.has_tool(call.name):
            result = ToolRunResult.failure(
                f"不存在名为 {call.name} 的工具，可用工具：{', '.join(self.runtime.tool_names())}"
            )
            self._respond(call, responded, result)
            return self._check_failure_threshold()

        tool = self.runtime.get_tool(call.name)

        # 在权限判定之前完成参数规范化：审批预览（describe）与实际执行（run）必须看到同一份
        # 经过 schema 默认值填充/类型校验的参数，否则用户在审批面板里看到的内容可能和真正执行的不一致。
        try:
            args = tool.validate(args)
        except InvalidToolArgumentsError as exc:
            self._respond(call, responded, ToolRunResult.failure(str(exc)))
            yield Notice(f"{call.name} 参数校验失败：{exc}", level="warning")
            return self._check_failure_threshold()

        repeats = self.session.record_call_fingerprint(call.name, args)
        if repeats >= self.settings.loop.max_repeated_calls:
            self._respond(
                call,
                responded,
                ToolRunResult.failure(
                    f"最近若干次调用中，完全相同的 {call.name} 调用已出现 {repeats} 次，"
                    "期间没有任何实质进展，已阻止以避免死循环。"
                ),
            )
            yield Notice(f"重复调用 {call.name} 且无进展，已终止任务。", level="error")
            return StopReason.REPEATED_CALLS
        if repeats == self.settings.loop.max_repeated_calls - 1:
            yield Notice(f"{call.name} 已被重复调用 {repeats} 次，请换一种方式。", level="warning")

        allowed = yield from self._resolve_permission(tool, args, call, responded)
        if not allowed:
            return self._check_failure_threshold()

        yield ToolStarted(call_id=call.id, name=call.name, args=args)
        self.session.start_tool_execution(
            call_id=call.id,
            tool_name=call.name,
            arguments=call.arguments,
        )
        started = time.monotonic()
        result = self.runtime.execute_tool(call.name, args, self.tool_ctx)
        duration = time.monotonic() - started

        self._respond(call, responded, result, duration=duration)
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
        outcome = self.runtime.check_permission(
            tool,
            args,
            mode=self.session.permissions.permission_mode,
            always_allowed=self.session.always_allowed,
        )

        if outcome.kind == "allow":
            return True

        if outcome.kind == "deny":
            self._respond(call, responded, ToolRunResult.failure(outcome.deny_message or "操作被拒绝。"))
            yield Notice(f"已拒绝 {tool.name}", level="warning")
            return False

        decision = yield ApprovalRequired(request=outcome.request)
        decision = decision if isinstance(decision, ApprovalDecision) else ApprovalDecision.DENY
        if not self.runtime.apply_permission_decision(decision, tool, args):
            self._respond(
                call,
                responded,
                ToolRunResult.failure(USER_DENIED_MSG),
            )
            yield Notice(f"已拒绝 {tool.name}", level="warning")
            return False
        return True

    def _respond(self, call: ToolCallBlock, responded: set[str], result: ToolRunResult, *, duration: float = 0.0) -> None:
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
        if self.session.tool_history.get(call.id) is None:
            self.session.start_tool_execution(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
            )
        self.session.finish_tool_execution(
            call_id=call.id,
            ok=ok,
            content=text,
            error=error,
            duration=duration,
        )
        self.session.add(ToolMessage.of(call.id))

    def _check_failure_threshold(self) -> StopReason | None:
        if self.session.consecutive_tool_failures >= self.settings.loop.max_consecutive_tool_failures:
            return StopReason.TOOL_FAILURES
        return None

    def _finish(self, reason: StopReason, final_text: str) -> TaskFinished:
        return TaskFinished(reason=reason, final_text=final_text, stats=self.session.stats())
