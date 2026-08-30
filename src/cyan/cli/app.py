"""交互式 REPL。

职责边界：把 Agent 产出的事件翻译成终端输出，把用户的审批意见回传给 Agent。
所有业务判断都在 core 与 security 层，这里不做决策。
"""

from __future__ import annotations

from dataclasses import replace
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
from ..errors import ConfigError
from ..llm.deepseek import DeepSeekClient
from ..logutil import get_logger
from ..security.permissions import PermissionManager
from ..security.types import PermissionMode
from ..session import Session
from ..session.store import DiskStore
from ..session.view import apply_system_prompt
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
    def __init__(
        self,
        settings: AgentSettings,
        console: Console | None = None,
        *,
        resume: str | None = None,
        continue_last: bool = False,
        permission_mode_override: PermissionMode | None = None,
    ) -> None:
        self.settings = settings
        self.renderer = Renderer(console)
        self.permissions = PermissionManager(settings.workspace)
        self.registry = build_default_registry()
        self.commands: CommandRegistry = build_default_commands()
        self._permission_mode_override = permission_mode_override
        self.session, warning = self._open_session(resume=resume, continue_last=continue_last)
        self.llm = DeepSeekClient(settings.llm, on_retry=self._on_llm_retry)
        self.runtime = Runtime.create(
            settings=settings,
            llm=self.llm,
            registry=self.registry,
            permissions=self.permissions,
            session=self.session,
            compact_policy=replace(settings.compact),
        )
        self._startup_warning = warning

    def _open_session(
        self, *, resume: str | None, continue_last: bool
    ) -> tuple[Session, str | None]:
        from ..session.branch import continue_session, load_session
        from ..session.store import resolve_session_id

        if resume:
            session_id = resolve_session_id(self.settings.workspace, resume)
            if session_id is None:
                raise ConfigError(f"找不到会话 {resume}")
            session, warning = load_session(self.settings.workspace, session_id)
            return self._after_load(session, warning)
        if continue_last:
            loaded = continue_session(self.settings.workspace)
            if loaded is not None:
                return self._after_load(*loaded)
        store = DiskStore.create(self.settings.workspace)
        session = Session.create(
            workspace=self.settings.workspace,
            system_prompt=build_system_prompt(self.settings.workspace),
            permission_mode=self.settings.cli.permission_mode,
            store=store,
            model=self.settings.llm.model,
        )
        return self._after_load(session, None)

    def _after_load(self, session: Session, warning: str | None) -> tuple[Session, str | None]:
        """resume 时刷新系统提示（日期等）；命令行 ``--mode`` 覆盖 meta 里的权限模式。"""
        apply_system_prompt(session, build_system_prompt(self.settings.workspace))
        if self._permission_mode_override is not None:
            session.permissions.permission_mode = self._permission_mode_override
            session.persist_head()
        return session, warning

    def attach_session(self, session: Session) -> None:
        """斜杠命令切换会话后，让 Runtime 指向同一对象。"""
        apply_system_prompt(session, build_system_prompt(self.settings.workspace))
        self.session = session
        self.runtime.session = session

    def start_new_session(self) -> Session:
        home = self.session.store.home if self.session.store is not None else None
        store = DiskStore.create(self.settings.workspace, home=home)
        session = Session.create(
            workspace=self.settings.workspace,
            system_prompt=build_system_prompt(self.settings.workspace),
            permission_mode=self.session.permissions.permission_mode,
            store=store,
            model=self.settings.llm.model,
        )
        self.attach_session(session)
        return session

    # ------------------------------------------------------------------ 入口
    def run_once(self, task: str) -> int:
        """非交互模式：执行单个任务后退出。"""
        reason = self._execute(task)
        return 0 if reason is StopReason.COMPLETED else 1

    def run_interactive(self) -> int:
        """REPL：读一行任务或斜杠命令，直到 /exit 或 Ctrl-D。"""
        self.renderer.banner(
            self.settings,
            [tool.name for tool in self.registry],
            self.session.permissions.permission_mode,
        )
        if self._startup_warning:
            self.renderer.notice(self._startup_warning, level="warning")
        if self.session.metadata.title:
            short = self.session.metadata.session_id[:8]
            self.renderer.notice(f"当前会话 {short}  {self.session.metadata.title}")
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
        """消费 Loop 事件流：普通事件只渲染；审批事件把用户选择 send 回去。"""
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
                reason = self._abort(stream)
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
        """把事件画到终端。只有 ``ApprovalRequired`` 会返回 ``ApprovalDecision``。"""
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
