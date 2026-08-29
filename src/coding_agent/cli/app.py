"""交互式 REPL。

职责边界：把 Agent 产出的事件翻译成终端输出，把用户的审批意见回传给 Agent。
所有业务判断都在 core 与 security 层，这里不做决策。
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from ..core.types import (
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
from ..core.prompts import build_system_prompt
from ..core.runtime import Runtime
from ..llm.deepseek import DeepSeekClient
from ..logutil import get_logger
from ..security.permissions import PermissionManager
from ..session import Session
from ..settings import AgentSettings
from ..tools.registry import build_default_registry
from .commands import CommandRegistry, build_default_commands
from .renderer import Renderer

try:  # 让输入框支持上下键历史与行编辑
    import readline  # noqa: F401
except ImportError:  # pragma: no cover - Windows 原生终端没有 readline
    pass

logger = get_logger("cli")


class App:
    def __init__(self, settings: AgentSettings, console: Console | None = None) -> None:
        self.settings = settings
        self.renderer = Renderer(console)
        self.permissions = PermissionManager(settings.workspace)
        self.registry = build_default_registry()
        self.commands: CommandRegistry = build_default_commands()
        self.session = Session.create(
            workspace=settings.workspace,
            system_prompt=build_system_prompt(settings.workspace),
            permission_mode=settings.cli.permission_mode,
        )
        self.llm = DeepSeekClient(settings.llm, on_retry=self._on_llm_retry)
        self.runtime = Runtime.create(
            settings=settings,
            llm=self.llm,
            registry=self.registry,
            permissions=self.permissions,
            session=self.session,
        )

    # ------------------------------------------------------------------ 入口
    def run_once(self, task: str) -> int:
        """非交互模式：执行单个任务后退出。"""
        reason = self._execute(task)
        return 0 if reason is StopReason.COMPLETED else 1

    def run_interactive(self) -> int:
        self.renderer.banner(
            self.settings,
            [tool.name for tool in self.registry],
            self.session.permissions.permission_mode,
        )
        while True:
            try:
                raw = self.renderer.console.input("\n[bold cyan]›[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                self.renderer.console.print("\n[dim]再见[/]")
                logger.info("再见")
                return 0

            if not raw:
                continue
            if raw.startswith("/"):
                if self._handle_command(raw):
                    return 0
                continue

            self._execute(raw)

    # ------------------------------------------------------------ 事件消费
    def _execute(self, task: str) -> StopReason:
        logger.info("收到任务：%s", task)
        stream = self.runtime.run(task)
        reply: Any = None
        reason = StopReason.FATAL_ERROR

        while True:
            try:
                event = stream.send(reply)
            except StopIteration:
                break
            except KeyboardInterrupt:
                self.renderer.notice("已中断", level="warning")
                reason = StopReason.USER_ABORT
                break

            reply = None
            try:
                if isinstance(event, TaskFinished):
                    reason = event.reason
                reply = self._render(event)
            except KeyboardInterrupt:
                reason = self._abort(stream)
                break

        return reason

    def _render(self, event: Any) -> Any:
        if isinstance(event, TaskStarted):
            return None
        if isinstance(event, Thinking):
            self.renderer.thinking(event.iteration)
            return None
        if isinstance(event, AssistantReply):
            self.renderer.assistant(event.text)
            return None
        if isinstance(event, ToolStarted):
            self.renderer.tool_started(event.name, event.args)
            return None
        if isinstance(event, ToolFinished):
            self.renderer.tool_finished(event.name, event.result, event.duration)
            return None
        if isinstance(event, ApprovalRequired):
            return self.renderer.ask_approval(event.request)
        if isinstance(event, Notice):
            self.renderer.notice(event.message, event.level)
            return None
        if isinstance(event, TaskFinished):
            self.renderer.task_finished(event.reason, event.stats)
            return None
        return None

    def _abort(self, stream: Any) -> StopReason:
        """把中断抛回 generator，让它清理未完成的工具调用。"""
        try:
            event = stream.throw(KeyboardInterrupt())
        except (StopIteration, KeyboardInterrupt):
            self.renderer.notice("已中断", level="warning")
            return StopReason.USER_ABORT

        if isinstance(event, TaskFinished):
            self.renderer.task_finished(event.reason, event.stats)
            stream.close()
            return event.reason

        stream.close()
        self.renderer.notice("已中断", level="warning")
        return StopReason.USER_ABORT

    # ------------------------------------------------------------ 斜杠命令
    def _handle_command(self, raw: str) -> bool:
        """返回 True 表示应当退出 REPL。新增命令不用改这里，去 cli/commands.py 注册即可。"""
        parts = raw.split()
        name = parts[0].lower()
        command = self.commands.get(name)
        if command is None:
            self.renderer.console.print(f"[yellow]未知命令 {name}，输入 /help 查看可用命令[/]")
            return False
        return command.handler(self, parts[1:])

    def _on_llm_retry(self, attempt: int, delay: float, message: str) -> None:
        self.renderer.notice(f"模型调用失败（{message}），{delay:.1f}s 后第 {attempt} 次重试", level="warning")
