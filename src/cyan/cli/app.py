"""交互式 REPL。

职责边界：把 Agent 产出的事件翻译成终端输出，把用户的审批意见回传给 Agent。
所有业务判断都在 core 与 security 层，这里不做决策。
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.console import Console

from ..core.types import (
    ApprovalRequired,
    AssistantReply,
    AssistantReplyDelta,
    Notice,
    StopReason,
    TaskFinished,
    TaskStarted,
    Thinking,
    ToolCallDelta,
    ToolFinished,
    ToolStarted,
)
from ..core.prompts import build_system_prompt
from ..core.runtime import Runtime
from ..errors import ConfigError
from ..llm.deepseek import DeepSeekClient
from ..logutil import get_logger
from ..memory.settings import auto_memory_enabled
from ..prompt.stack import PromptStack
from ..security.permissions import PermissionManager, initial_permission_mode
from ..security.types import PermissionMode
from ..session import Session
from ..session.paths import cyan_home
from ..session.store import DiskStore
from ..session.view import apply_system_prompt
from ..settings import AgentSettings
from ..tools.registry import build_default_registry
from .commands import CommandRegistry, build_default_commands
from .completion import InputCompleter, at_reference_prefix
from .file_refs import extract_file_refs
from .renderer import Renderer

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
        home = cyan_home()
        self.permissions = PermissionManager(settings.workspace, home=home)
        self.registry = build_default_registry()
        self.commands: CommandRegistry = build_default_commands()
        self._permission_mode_override = permission_mode_override
        self.session, warning = self._open_session(resume=resume, continue_last=continue_last)
        self.llm = DeepSeekClient(settings.llm, on_retry=self._on_llm_retry)
        home = self.session.store.home if self.session.store is not None else cyan_home()
        self._prompt_session: PromptSession[str] = self._build_prompt_session(home)
        self.runtime = Runtime.create(
            settings=settings,
            llm=self.llm,
            registry=self.registry,
            permissions=self.permissions,
            session=self.session,
            compact_policy=replace(settings.compact),
            prompt_stack=PromptStack(
                workspace=settings.workspace,
                home=home,
                auto_memory=auto_memory_enabled(),
            ),
        )
        self._startup_warning = warning
        # /skill <name> 设的"这一次额外强调"提醒：只消费一次，紧跟着下一条任务的
        # UserMessage 一起发出去，跟 4 个 skill 常驻自动注入是两件独立的事。
        self._pending_skill_reminder: str | None = None

    def _build_prompt_session(self, home: Path) -> PromptSession[str]:
        """输入 "/" 或 "@" 后不用按 Tab，随打字（含删除字符）实时更新候选列表，
        可用方向键选择；历史记录持久化到 ``<home>/history``，跨会话保留。

        没用内置的 ``complete_while_typing``：它只在插入字符时才重新补全，删除
        字符不会重算（比如打完 "/he" 删掉一个字符变成 "/h"，候选就卡死不动了）；
        而且只要这个开关是 True，不管当前有没有候选，布局都会预留一块
        ``reserve_space_for_menu`` 空白菜单区域，看起来像个挥之不去的空灰框。
        改成自己监听 ``on_text_changed``（插入、删除都会触发）手动
        ``start_completion()``——没有候选时 prompt_toolkit 会把 ``complete_state``
        收回 ``None``，那块预留空白也就跟着消失了。
        """
        home.mkdir(parents=True, exist_ok=True)
        session: PromptSession[str] = PromptSession(
            completer=InputCompleter(self.commands, self.settings.workspace),
            complete_while_typing=False,
            history=FileHistory(str(home / "history")),
        )

        def _retrigger_completion(_: Any) -> None:
            buffer = session.default_buffer
            text = buffer.text
            if text.startswith("/") or at_reference_prefix(text) is not None:
                buffer.start_completion(select_first=False)

        session.default_buffer.on_text_changed += _retrigger_completion
        return session

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
            permission_mode=initial_permission_mode(
                self.settings.cli.permission_mode,
                override=self._permission_mode_override,
                configured=self.permissions.configured_mode,
            ),
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
    def run_interactive(self) -> int:
        """REPL：读一行任务或斜杠命令，直到 /exit 或 Ctrl-D。"""
        self.renderer.banner(
            self.settings,
            [tool.name for tool in self.registry],
            self.session.permissions.permission_mode,
            instruction_labels=self._instruction_labels(),
        )
        if self._startup_warning:
            self.renderer.notice(self._startup_warning, level="warning")
        if self.session.metadata.title:
            short = self.session.metadata.session_id[:8]
            self.renderer.notice(f"当前会话 {short}  {self.session.metadata.title}")
        while True:
            self.renderer.console.print()
            try:
                raw = self._prompt_session.prompt(HTML("<b><ansicyan>› </ansicyan></b>")).strip()
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
        file_refs = extract_file_refs(task, self.settings.workspace, self.runtime.tool_limits)
        if file_refs:
            self.renderer.notice("已附加文件：" + "、".join(block.path for block in file_refs))
        effective_task = task
        if self._pending_skill_reminder is not None:
            effective_task = f"{self._pending_skill_reminder}\n\n{task}"
            self._pending_skill_reminder = None
        stream = self.runtime.run(effective_task, file_refs=file_refs)
        reply: Any = None
        reason = StopReason.FATAL_ERROR
        # 只用于结束面板里展示「用时」，不进 session.stats()——那是模型/工具调用次数的
        # 领域统计，跟这次任务花了多少墙钟时间是两件事，不该混进同一个持久化结构里。
        started_at = time.monotonic()
        # 上一次渲染的事件是不是 Thinking：是的话，下一次 send() 大概率会卡在等模型
        # 返回第一个 chunk 上，用转圈动画包一下；send() 本身仍在主线程同步执行，
        # 不影响 Ctrl-C 中断的现有语义。
        waiting_for_model = False

        while True:
            try:
                if waiting_for_model:
                    with self.renderer.waiting_spinner():
                        event = stream.send(reply)
                else:
                    event = stream.send(reply)
            except StopIteration:
                break
            except KeyboardInterrupt:
                reason = self._abort(stream, started_at=started_at)
                break

            waiting_for_model = isinstance(event, Thinking)
            reply = None
            try:
                if isinstance(event, TaskFinished):
                    reason = event.reason
                reply = self._render(event, started_at=started_at)
            except KeyboardInterrupt:
                reason = self._abort(stream, started_at=started_at)
                break

        return reason

    def _render(self, event: Any, *, started_at: float | None = None) -> Any:
        """把事件画到终端。只有 ``ApprovalRequired`` 会返回 ``ApprovalDecision``。

        除了两种流式分片事件本身会管理 ``Live`` 的生命周期，其它任何事件渲染前
        都先收尾一下：流式中途报错（比如 ``LLMError``）会跳过 ``AssistantReply``/
        ``ToolStarted`` 直接落到 ``Notice``，如果不在这里兜底收尾，没打完的
        ``Live`` 就会一直挂着，跟这条 ``Notice`` 的输出挤在一起。
        """
        if not isinstance(event, (AssistantReplyDelta, ToolCallDelta)):
            self.renderer.stop_live_preview()
        if isinstance(event, TaskStarted):
            return None
        if isinstance(event, Thinking):
            self.renderer.thinking(event.iteration)
            return None
        if isinstance(event, AssistantReplyDelta):
            self.renderer.assistant_delta(event.text)
            return None
        if isinstance(event, ToolCallDelta):
            self.renderer.tool_call_delta(event.index, event.call_id, event.name, event.arguments_delta)
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
            elapsed = None if started_at is None else time.monotonic() - started_at
            self.renderer.task_finished(event.reason, event.stats, elapsed=elapsed)
            return None
        return None

    def _abort(self, stream: Any, *, started_at: float | None = None) -> StopReason:
        """把中断抛回 generator，让它清理未完成的工具调用。"""
        self.renderer.abort_live()
        try:
            event = stream.throw(KeyboardInterrupt())
        except (StopIteration, KeyboardInterrupt):
            self.renderer.notice("已中断", level="warning")
            return StopReason.USER_ABORT

        if isinstance(event, TaskFinished):
            elapsed = None if started_at is None else time.monotonic() - started_at
            self.renderer.task_finished(event.reason, event.stats, elapsed=elapsed)
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

    def _instruction_labels(self) -> list[str]:
        """当前已加载的文件指令层标题，供 banner 使用。

        Skill 不逐条列出：skill 数量一多，横幅那一行就被塞满、还得截断，价值不大——
        这里只汇总成"Skill 已启用 N 个"一句话，具体清单跟启用/禁用状态看 ``/skills``。
        （`prompt_stack.extra` 里出现的 Skill 层本来就已经是启用中的——被
        ``/skills disable`` 关掉的不会生成层，见 ``prompt/skills.py.load_skill_layers``。）
        """
        from ..prompt.types import PromptLayerKind

        self.runtime.prompt_stack.refresh_files()
        labels: list[str] = []
        skill_count = 0
        for layer in self.runtime.prompt_stack.extra:
            if layer.kind is PromptLayerKind.SKILL:
                skill_count += 1
                continue
            labels.append(layer.title)
        if skill_count:
            labels.append(f"Skill 已启用 {skill_count} 个")
        return labels

    def _on_llm_retry(self, attempt: int, delay: float, message: str) -> None:
        self.renderer.notice(f"模型调用失败（{message}），{delay:.1f}s 后第 {attempt} 次重试", level="warning")
