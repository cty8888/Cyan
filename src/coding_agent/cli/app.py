"""交互式 REPL。

职责边界：把 Agent 产出的事件翻译成终端输出，把用户的审批意见回传给 Agent。
所有业务判断都在 core 与 security 层，这里不做决策。
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from ..config import Config
from ..core.agent import Agent
from ..core.events import (
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
from ..core.prompts import build_system_prompt
from ..core.session import Session
from ..llm.deepseek import DeepSeekClient
from ..logutil import get_logger
from ..constants.security.mode_labels import MODE_LABELS
from ..security.modes import PermissionMode
from ..security.permissions import PermissionManager
from ..tools.registry import build_default_registry
from .renderer import Renderer

try:  # 让输入框支持上下键历史与行编辑
    import readline  # noqa: F401
except ImportError:  # pragma: no cover - Windows 原生终端没有 readline
    pass

logger = get_logger("cli")

HELP_TEXT = """可用命令：
  /help          显示本帮助
  /tools         列出已注册的工具
  /mode <模式>   切换权限模式：plan / default / accept_edits / bypass
  /usage         显示本会话的 token 与调用统计
  /clear         清空对话历史，开始新会话
  /cwd           显示当前工作目录
  /exit, /quit   退出

直接输入自然语言即可下达任务。任务执行中按 Ctrl-C 可以中断。"""


class App:
    def __init__(self, config: Config, console: Console | None = None):
        self.config = config
        self.renderer = Renderer(console)
        self.permissions = PermissionManager(config.workspace)
        self.registry = build_default_registry()
        self.session = Session.create(
            workspace=config.workspace,
            system_prompt=build_system_prompt(config.workspace),
            permission_mode=config.permission_mode,
        )
        self.llm = DeepSeekClient(config, on_retry=self._on_llm_retry)
        self.agent = Agent(
            config=config,
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
        self.renderer.banner(self.config, [tool.name for tool in self.registry])
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
        stream = self.agent.run(task)
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
        if isinstance(event, AssistantMessage):
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
        """返回 True 表示应当退出 REPL。"""
        command = raw.split()[0].lower()
        console = self.renderer.console

        if command in {"/exit", "/quit"}:
            console.print("[dim]再见[/]")
            logger.info("再见")
            return True
        if command == "/help":
            console.print(HELP_TEXT)
        elif command == "/tools":
            for tool in self.registry:
                console.print(
                    f"  [bold]{tool.name}[/] "
                    f"[dim]({tool.capability.value}/{tool.risk.value})[/] — {tool.description}"
                )
        elif command == "/mode":
            parts = raw.split()
            if len(parts) != 2:
                console.print("[yellow]用法：/mode plan|default|accept_edits|bypass[/]")
            else:
                try:
                    mode = PermissionMode(parts[1])
                    self.config.permission_mode = mode
                    self.session.permissions.permission_mode = mode
                    label = MODE_LABELS[mode]
                    console.print(f"[dim]已切换至 {label}[/]")
                    logger.info("切换权限模式：%s", mode.value)
                except ValueError:
                    console.print("[yellow]无效权限模式, 可选: plan / default / accept_edits / bypass[/]")
        elif command == "/usage":
            stats = self.session.stats()
            console.print(
                f"  模型调用 {stats['llm_calls']} 次 · 工具调用 {stats['tool_calls']} 次\n"
                f"  tokens：输入 {stats['prompt_tokens']} / 输出 {stats['completion_tokens']}"
                f" / 合计 {stats['total_tokens']}\n"
                f"  历史消息 {stats['messages']} 条"
            )
        elif command == "/clear":
            self.session.clear()
            console.print("[dim]已清空对话历史[/]")
        elif command == "/cwd":
            console.print(f"  {self.config.workspace}")
        else:
            console.print(f"[yellow]未知命令 {command}，输入 /help 查看可用命令[/]")
        return False

    def _on_llm_retry(self, attempt: int, delay: float, message: str) -> None:
        self.renderer.notice(f"模型调用失败（{message}），{delay:.1f}s 后第 {attempt} 次重试", level="warning")
