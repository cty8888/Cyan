"""Agent Loop：驱动 LLM ↔ 工具 循环，通过事件流与 CLI 交互。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generator

from ..errors import AgentError, InvalidToolArgumentsError, LLMContextOverflowError, LLMError
from ..llm.parser import parse_tool_arguments
from ..llm.types import AssistantMessage, ContinueMessage, ToolCallBlock, ToolMessage, UserMessage
from ..security.messages import USER_DENIED_MSG
from ..security.types import ApprovalDecision
from ..session import WorkspaceAccess
from ..tools.types import ToolContext, ToolRunResult
from .prompts import EMPTY_REPLY_CONTINUE_MSG, TRUNCATION_CONTINUE_MSG
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
        """驱动「调用模型 → 执行工具 → 再调用」直到完成或触发终止条件。"""
        self.session.state.current_task = task
        self.session.add(UserMessage.of(task))
        self.session.consecutive_tool_failures = 0
        self.session.reset_repeat_tracking()
        yield TaskStarted(task=task)

        final_text = ""
        schemas = self.runtime.schemas_for_mode()
        incomplete_replies = 0

        try:
            for iteration in range(1, self.settings.loop.max_iterations + 1):
                yield Thinking(iteration=iteration)

                yield from self._shrink_context()

                try:
                    response = self.runtime.call_llm(self.runtime.messages_for_request(), tools=schemas)
                except LLMContextOverflowError as exc:
                    recovered = yield from self._compact_after_overflow()
                    if not recovered:
                        yield Notice(f"模型调用失败：{exc}", level="error")
                        yield self._finish(StopReason.FATAL_ERROR, final_text)
                        return
                    try:
                        response = self.runtime.call_llm(
                            self.runtime.messages_for_request(), tools=schemas
                        )
                    except LLMError as retry_exc:
                        yield Notice(f"模型调用失败：{retry_exc}", level="error")
                        yield self._finish(StopReason.FATAL_ERROR, final_text)
                        return
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
                if tool_calls:
                    incomplete_replies = 0
                    stop_reason = yield from self._run_tool_calls(tool_calls)
                    if stop_reason is not None:
                        yield self._finish(stop_reason, final_text)
                        return
                    continue

                if _is_truncated_finish(response.finish_reason) or not text:
                    incomplete_replies += 1
                    limit = self.settings.loop.max_consecutive_tool_failures
                    truncated = _is_truncated_finish(response.finish_reason)
                    if incomplete_replies >= limit:
                        kind = "输出被截断" if truncated else "空回复"
                        yield Notice(
                            f"连续 {incomplete_replies} 次{kind}，已停止。",
                            level="warning",
                        )
                        yield self._finish(StopReason.MAX_ITERATIONS, final_text)
                        return
                    if truncated:
                        self.session.add(ContinueMessage.of(TRUNCATION_CONTINUE_MSG))
                        yield Notice("模型输出被截断，已要求继续。", level="warning")
                    else:
                        self.session.add(ContinueMessage.of(EMPTY_REPLY_CONTINUE_MSG))
                        yield Notice("模型没有给出回复，已要求继续。", level="warning")
                    continue

                yield self._finish(StopReason.COMPLETED, final_text)
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
        # 已写回 tool 结果的 call id。本批每条 tool_call 都必须有对应回复，
        # 否则同一会话里下一次请求会带着残缺配对去打 API。
        responded: set[str] = set()
        try:
            for call in tool_calls:
                stop_reason = yield from self._run_single_call(call, responded)
                if stop_reason is not None:
                    self._respond_unanswered(
                        tool_calls,
                        responded,
                        "任务已终止，该工具调用未执行。",
                    )
                    return stop_reason
            return None
        except KeyboardInterrupt:
            self._respond_unanswered(
                tool_calls,
                responded,
                "用户中断了任务，该工具调用未执行。",
            )
            raise

    def _run_single_call(
        self,
        call: ToolCallBlock,
        responded: set[str],
    ) -> Generator[AgentEvent, Any, StopReason | None]:
        """处理一条工具调用：解析参数、鉴权、执行，并把结果写回会话。

        任何出口都要 ``_respond``，保证下轮请求里每条 tool_call 都有对应回复。
        返回非 ``None`` 的 ``StopReason`` 时，外层会结束整次任务。
        """
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

        allowed = yield from self._resolve_permission(tool, args, call, responded)
        if not allowed:
            repeats = self.session.record_call_fingerprint(call.name, args)
            if repeats >= self.settings.loop.max_repeated_calls:
                yield Notice(f"重复调用 {call.name} 且无进展，已终止任务。", level="error")
                return StopReason.REPEATED_CALLS
            return None

        repeats = self.session.record_call_fingerprint(call.name, args)
        if repeats >= self.settings.loop.max_repeated_calls:
            self._respond(
                call,
                responded,
                ToolRunResult.failure(
                    f"连续 {repeats} 次调用完全相同的 {call.name} 且中间没有成功进展，"
                    "已阻止以避免死循环。"
                ),
            )
            yield Notice(f"重复调用 {call.name} 且无进展，已终止任务。", level="error")
            return StopReason.REPEATED_CALLS
        if repeats == self.settings.loop.max_repeated_calls - 1:
            yield Notice(f"{call.name} 已被重复调用 {repeats} 次，请换一种方式。", level="warning")

        yield ToolStarted(call_id=call.id, name=call.name, args=args)
        self.session.start_tool_execution(
            call_id=call.id,
            tool_name=call.name,
            arguments=json.dumps(args, ensure_ascii=False),
        )
        started = time.monotonic()
        result = self.runtime.execute_tool(call.name, args, self.tool_ctx)
        duration = time.monotonic() - started

        self._respond(call, responded, result, duration=duration)
        if result.ok:
            self.session.reset_repeat_tracking()
        yield ToolFinished(call_id=call.id, name=call.name, result=result, duration=duration)

        return self._check_failure_threshold()

    def _resolve_permission(
        self,
        tool: Any,
        args: dict[str, Any],
        call: ToolCallBlock,
        responded: set[str],
    ) -> Generator[AgentEvent, Any, bool]:
        """解析权限：放行、硬拒绝，或 yield 审批事件等人选择。

        ``yield ApprovalRequired`` 会把 Loop 挂起；CLI ``stream.send(decision)``
        的返回值就是用户的 ``ApprovalDecision``。类型不对时按拒绝处理。
        """
        outcome = self.runtime.check_permission(
            tool,
            args,
            mode=self.session.permissions.permission_mode,
            always_allowed=self.session.always_allowed,
        )

        if outcome.kind == "allow":
            return True

        if outcome.kind == "deny":
            self._respond(
                call,
                responded,
                ToolRunResult.failure(
                    outcome.deny_message or "操作被拒绝。",
                    counts_as_failure=False,
                ),
            )
            yield Notice(f"已拒绝 {tool.name}", level="warning")
            return False

        decision = yield ApprovalRequired(request=outcome.request)
        decision = decision if isinstance(decision, ApprovalDecision) else ApprovalDecision.DENY
        force = bool(outcome.request and outcome.request.force)
        if not self.runtime.apply_permission_decision(decision, tool, args, force=force):
            self._respond(
                call,
                responded,
                ToolRunResult.failure(USER_DENIED_MSG, counts_as_failure=False),
            )
            yield Notice(f"已拒绝 {tool.name}", level="warning")
            return False
        # 刚批准的这次从 1 计，避免「拒绝两次再同意」被当成第三次空转。
        self.session.reset_repeat_tracking()
        return True

    def _respond(self, call: ToolCallBlock, responded: set[str], result: ToolRunResult, *, duration: float = 0.0) -> None:
        """把工具结果写入 tool_history 和 messages，并记入 ``responded``。

        未真正执行过的路径（参数错误、权限拒绝、中断）还没有 start 记录，这里补一条。
        """
        if self.session.tool_history.get(call.id) is None:
            self.session.start_tool_execution(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
            )
        self.session.finish_tool_execution(
            call_id=call.id,
            ok=result.ok,
            content=result.to_model_text(),
            error=result.error,
            duration=duration,
            counts_as_failure=bool(not result.ok and result.counts_as_failure),
        )
        self.session.add(ToolMessage.of(call.id))
        responded.add(call.id)

    def _respond_unanswered(
        self,
        tool_calls: list[ToolCallBlock],
        responded: set[str],
        error: str,
    ) -> None:
        """给本批尚未写回的 tool_call 补一条失败结果，保证会话配对完整。"""
        for call in tool_calls:
            if call.id not in responded:
                self._respond(
                    call,
                    responded,
                    ToolRunResult.failure(error, counts_as_failure=False),
                )

    def _pair_pending_tool_calls(self, error: str) -> None:
        """最近一条带 tool_calls 的 assistant，若还有未回复的 call，补上失败结果。

        覆盖 ``_run_tool_calls`` 进不去的窗口：assistant 已入会话、
        正在 ``yield AssistantReply`` 时被中断。
        """
        assistant = next(
            (
                message
                for message in reversed(self.session.messages)
                if isinstance(message, AssistantMessage) and message.tool_calls
            ),
            None,
        )
        if assistant is None:
            return
        responded: set[str] = set()
        for message in self.session.messages:
            if not isinstance(message, ToolMessage):
                continue
            block = message.tool_result
            if block and block.tool_call_id:
                responded.add(block.tool_call_id)
        self._respond_unanswered(assistant.tool_calls, responded, error)

    def _shrink_context(self) -> Generator[AgentEvent, Any, bool]:
        """出门前按阈值尽量压。保留段仍超窗时会降到更少轮，最多压 ``keep+1`` 次。"""
        did = False
        limit = max(1, self.runtime.compact_policy.keep_recent_turns + 1)
        for _ in range(limit):
            if not self.runtime.needs_compact():
                break
            if not did:
                yield Notice("上下文接近上限，正在压缩…")
            if not self.runtime.compact():
                if not did:
                    yield Notice("压缩失败，继续使用原文。", level="warning")
                break
            did = True
        if did:
            yield Notice("已压缩较早的对话历史。")
        return did

    def _compact_after_overflow(self) -> Generator[AgentEvent, Any, bool]:
        """估算漏检、API 已拒时：强制紧急压缩（不留原文轮），再按阈值尽量再压。"""
        yield Notice("上下文超出模型窗口，正在压缩后重试…", level="warning")
        if not self.runtime.compact(max_keep=0):
            yield Notice("压缩失败，无法缩小上下文。", level="error")
            return False
        limit = max(1, self.runtime.compact_policy.keep_recent_turns)
        for _ in range(limit):
            if not self.runtime.needs_compact():
                break
            if not self.runtime.compact():
                break
        yield Notice("已压缩较早的对话历史。")
        return True

    def _check_failure_threshold(self) -> StopReason | None:
        """连续失败达到上限则终止任务；计数在 ``Session.finish_tool_execution`` 里更新。"""
        if self.session.consecutive_tool_failures >= self.settings.loop.max_consecutive_tool_failures:
            return StopReason.TOOL_FAILURES
        return None

    def _finish(self, reason: StopReason, final_text: str) -> TaskFinished:
        """组装任务结束事件；离开前补齐尚未回复的 tool_call。"""
        error = (
            "用户中断了任务，该工具调用未执行。"
            if reason is StopReason.USER_ABORT
            else "任务已终止，该工具调用未执行。"
        )
        self._pair_pending_tool_calls(error)
        return TaskFinished(reason=reason, final_text=final_text, stats=self.session.stats())


_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


def _is_truncated_finish(reason: str | None) -> bool:
    """厂商把补全打到 token 上限标成 length / max_tokens，不是任务完成。"""
    return (reason or "").lower() in _TRUNCATED_FINISH_REASONS
