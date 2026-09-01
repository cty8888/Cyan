"""交互式 REPL 的斜杠命令。

设计上与 ``tools/registry.py`` 保持一致：新增一个命令 = 写一个 handler 函数 + 在
``build_default_commands()`` 里注册一行，不需要再去改一条 if/elif 链，``/help`` 的
文本也会跟着自动列出新命令，不需要手动同步一份 ``HELP_TEXT`` 字符串。

会话里改行为策略（压缩阈值、保留轮数、轮次上限、工具截断等）只写 ``app.runtime``
上的副本（``compact_policy`` / ``loop_limits`` / ``tool_limits`` / ``context_policy``），
不改 ``app.settings``——见 ``docs/architecture.md``「运行时策略与斜杠命令」。
``/compact``、``/loop``、``/tools``、``/context`` 四个命令共用 ``_show_policy`` /
``_set_policy_field`` 这对小工具，按 dataclass 字段类型做字符串转换。
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from rich.console import Console
from rich.prompt import Prompt

from ..logutil import get_logger
from ..security.types import PermissionMode
from ..session.events import (
    COMPACT_REASON_MANUAL,
    COMPACT_REASON_SUMMARIZE_FROM,
    COMPACT_REASON_SUMMARIZE_UP,
)
from .renderer import MODE_LABELS, render_todo_lines

if TYPE_CHECKING:
    from .app import App

logger = get_logger("cli")

# handler 返回 True 表示应当退出 REPL。
CommandHandler = Callable[["App", list[str]], bool]


def _show_policy(console: Console, title: str, policy: Any) -> None:
    """按声明顺序打印一个策略 dataclass 的所有字段，供 ``/loop`` 等命令查看当前值用。"""
    console.print(f"[bold]{title}[/]")
    for f in dataclasses.fields(policy):
        console.print(f"  {f.name:<28} {getattr(policy, f.name)}")


def _set_policy_field(console: Console, policy: Any, name: str, raw_value: str) -> bool:
    """把字符串按字段声明类型转换后写到策略对象上（原地改，策略对象本身就是 Runtime 持有的副本）。

    用 ``typing.get_type_hints`` 而不是直接读 ``Field.type``——策略模块都开了
    ``from __future__ import annotations``，``Field.type`` 拿到的是没解析过的字符串。
    """
    hints = typing.get_type_hints(type(policy))
    if name not in hints:
        options = ", ".join(hints)
        console.print(f"[yellow]没有这个字段：{name}，可选：{options}[/]")
        return False
    field_type = hints[name]
    try:
        if field_type is bool:
            value: Any = raw_value.lower() in {"1", "true", "on", "yes"}
        elif field_type is int:
            value = int(raw_value)
        elif field_type is float:
            value = float(raw_value)
        else:
            value = raw_value
    except ValueError:
        console.print(f"[yellow]{name} 需要 {field_type.__name__} 类型的值，收到：{raw_value}[/]")
        return False
    setattr(policy, name, value)
    console.print(f"[dim]{name} = {value}[/]")
    return True


@dataclass(frozen=True)
class SlashCommand:
    """一条斜杠命令的声明：名称、用法、说明、处理函数，以及可选别名。"""

    name: str
    usage: str
    description: str
    handler: CommandHandler
    aliases: tuple[str, ...] = field(default_factory=tuple)


class CommandRegistry:
    """持有已注册的斜杠命令，按名称（含别名）分发。"""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._order: list[str] = []

    def register(self, command: SlashCommand) -> SlashCommand:
        """按主名和别名注册；名称冲突立即失败。"""
        for name in (command.name, *command.aliases):
            if name in self._commands:
                raise ValueError(f"命令名重复：{name}")
            self._commands[name] = command
        self._order.append(command.name)
        return command

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name)

    def __iter__(self) -> Iterator[SlashCommand]:
        """按注册顺序遍历，每个命令只出现一次（不重复展开别名）。"""
        by_name = {c.name: c for c in self._commands.values()}
        for name in self._order:
            yield by_name[name]


def build_help_text(registry: CommandRegistry) -> str:
    """根据当前注册表生成 /help 文本，新增命令不必再手写一份列表。"""
    lines = ["可用命令："]
    for command in registry:
        label = command.usage
        if command.aliases:
            label += f", {', '.join(command.aliases)}"
        lines.append(f"  {label:<16} {command.description}")
    lines.append("")
    lines.append("直接输入自然语言即可下达任务。任务执行中按 Ctrl-C 可以中断。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 内置命令 handler


def _cmd_help(app: App, args: list[str]) -> bool:
    app.renderer.console.print(build_help_text(app.commands))
    return False


def _cmd_tools(app: App, args: list[str]) -> bool:
    """不带参数列出已注册工具（原有行为）；``limits``/``<字段> <值>`` 查看或改
    ``runtime.tool_limits``（会话中途的副本，不动 ``AgentSettings.tools``）。
    """
    console = app.renderer.console
    if not args:
        for tool in app.registry:
            console.print(
                f"  [bold]{tool.name}[/] "
                f"[dim]({tool.capability.value})[/] — {tool.description}"
            )
        return False
    if args[0] == "limits" and len(args) == 1:
        _show_policy(console, "工具限制（本会话）", app.runtime.tool_limits)
        return False
    if len(args) == 2:
        _set_policy_field(console, app.runtime.tool_limits, args[0], args[1])
        return False
    console.print("[yellow]用法：/tools | /tools limits | /tools <字段> <值>[/]")
    return False


def _cmd_mode(app: App, args: list[str]) -> bool:
    console = app.renderer.console
    if len(args) != 1:
        console.print("[yellow]用法：/mode plan|default|accept_edits[/]")
        return False
    try:
        mode = PermissionMode(args[0])
    except ValueError:
        console.print("[yellow]无效权限模式, 可选: plan / default / accept_edits[/]")
        return False
    app.session.permissions.permission_mode = mode
    app.session.persist_head()
    console.print(f"[dim]已切换至 {MODE_LABELS[mode]}[/]")
    logger.info("切换权限模式：%s", mode.value)
    return False


def _cmd_permissions(app: App, args: list[str]) -> bool:
    """列出或增删声明式规则。新增写入 local；删除可动 local / 项目 / 用户。"""
    from ..security.rule_syntax import parse_rule
    from ..security.settings_file import add_local_rule, remove_rule

    console = app.renderer.console
    if not args:
        rules = app.permissions.ruleset.rules
        if not rules:
            console.print("[dim]当前没有权限规则[/]")
            return False
        for rule in rules:
            lock = "" if rule.removable else "  [dim](内置)[/]"
            console.print(f"  [bold]{rule.kind:<5}[/] {rule.raw:<28} [dim]{rule.source}[/]{lock}")
        return False

    action = args[0].lower()
    raw = " ".join(args[1:]).strip()
    if action in {"allow", "ask", "deny"}:
        if not raw:
            console.print("[yellow]用法：/permissions allow|ask|deny <规则>[/]")
            return False
        try:
            parse_rule(raw)
        except ValueError as exc:
            console.print(f"[yellow]{exc}[/]")
            return False
        add_local_rule(app.settings.workspace, action, raw)  # type: ignore[arg-type]
        app.permissions.reload()
        console.print(f"[dim]已写入 local：{action} {raw}[/]")
        logger.info("permissions %s %s", action, raw)
        return False
    if action == "remove":
        if not raw:
            console.print("[yellow]用法：/permissions remove <规则>[/]")
            return False
        status, source = remove_rule(app.settings.workspace, raw, home=app.permissions.home)
        if status == "builtin":
            console.print("[yellow]不能删除内置规则[/]")
            return False
        if status == "missing":
            console.print(f"[yellow]没有可删除的规则 {raw}[/]")
            return False
        app.permissions.reload()
        console.print(f"[dim]已从 {source} 删除 {raw}[/]")
        logger.info("permissions remove %s from %s", raw, source)
        return False
    console.print("[yellow]用法：/permissions [allow|ask|deny|remove] <规则>[/]")
    return False


def _cmd_usage(app: App, args: list[str]) -> bool:
    stats = app.session.stats()
    app.renderer.console.print(
        f"  模型调用 {stats['llm_calls']} 次 · 工具调用 {stats['tool_calls']} 次\n"
        f"  tokens：输入 {stats['prompt_tokens']} / 输出 {stats['completion_tokens']}"
        f" / 合计 {stats['total_tokens']}\n"
        f"  历史消息 {stats['messages']} 条"
    )
    return False


def _cmd_instructions(app: App, args: list[str]) -> bool:
    """列出当前 Prompt Layer（身份 + cyan.md + MEMORY.md），不含类型文件全文。"""
    from ..llm.types import SystemMessage

    stack = app.runtime.prompt_stack
    identity_text = ""
    if app.session.messages and isinstance(app.session.messages[0], SystemMessage):
        identity_text = app.session.messages[0].text or ""
    stack.set_identity(identity_text)
    stack.refresh_files()
    layers = stack.layers()
    if not layers:
        app.renderer.console.print("[dim]当前没有指令层[/]")
        return False
    for layer in layers:
        source = str(layer.source) if layer.source is not None else "（内置）"
        extra = "  [yellow]已截断[/]" if layer.truncated else ""
        app.renderer.console.print(
            f"  [bold]{layer.title}[/]  [dim]{source}[/]  {len(layer.text)} 字{extra}"
        )
    return False


def _cmd_memory(app: App, args: list[str]) -> bool:
    """列出磁盘上的自动记忆文件，与 /instructions 分开。"""
    from ..memory.settings import auto_memory_enabled
    from ..memory.store import list_memory_files, memory_dir

    if not auto_memory_enabled():
        app.renderer.console.print("[dim]自动记忆已关闭（CYAN_DISABLE_AUTO_MEMORY）[/]")
        return False
    directory = memory_dir(app.settings.workspace)
    items = list_memory_files(app.settings.workspace)
    app.renderer.console.print(f"  [dim]{directory}[/]")
    if not items:
        app.renderer.console.print("[dim]还没有自动记忆文件[/]")
        return False
    for name, size in items:
        app.renderer.console.print(f"  [bold]{name}[/]  {size} 字节")
    return False


def _cmd_stream(app: App, args: list[str]) -> bool:
    """查看或切换流式输出。直接改 ``app.settings.llm``——``DeepSeekClient`` 持有的是
    同一个 ``LLMSettings`` 对象，下一次模型调用立刻生效，不需要重建客户端。
    """
    console = app.renderer.console
    if not args:
        state = "开" if app.settings.llm.stream else "关"
        console.print(f"[dim]流式输出当前：{state}（/stream on|off 切换）[/]")
        return False
    value = args[0].lower()
    if value not in {"on", "off"}:
        console.print("[yellow]用法：/stream on|off[/]")
        return False
    app.settings.llm.stream = value == "on"
    console.print(f"[dim]流式输出已{'开启' if app.settings.llm.stream else '关闭'}[/]")
    logger.info("切换流式输出：%s", app.settings.llm.stream)
    return False


def _cmd_loop(app: App, args: list[str]) -> bool:
    """查看或修改 ``runtime.loop_limits``（轮次上限等），不动 ``AgentSettings.loop``。"""
    console = app.renderer.console
    if not args:
        _show_policy(console, "循环限制（本会话）", app.runtime.loop_limits)
        return False
    if len(args) != 2:
        console.print("[yellow]用法：/loop | /loop <字段> <值>[/]")
        return False
    _set_policy_field(console, app.runtime.loop_limits, args[0], args[1])
    return False


def _cmd_context(app: App, args: list[str]) -> bool:
    """查看或修改 ``runtime.context_policy``（发给模型时单条工具结果的截断长度）。"""
    console = app.renderer.console
    if not args:
        _show_policy(console, "上下文策略（本会话）", app.runtime.context_policy)
        return False
    if len(args) != 2:
        console.print("[yellow]用法：/context | /context <字段> <值>[/]")
        return False
    _set_policy_field(console, app.runtime.context_policy, args[0], args[1])
    return False


def _cmd_model(app: App, args: list[str]) -> bool:
    """查看或切换模型。直接改 ``app.settings.llm.model``——跟 ``/stream`` 一样，
    ``DeepSeekClient`` 实时读同一个 ``LLMSettings`` 对象，不需要重建客户端。
    不做模型名校验：这是目前唯一在用的后端，改了下一次调用就生效。
    """
    console = app.renderer.console
    if not args:
        console.print(f"[dim]当前模型：{app.settings.llm.model}（/model <名字> 切换）[/]")
        return False
    app.settings.llm.model = args[0]
    console.print(f"[dim]模型已切换为 {args[0]}[/]")
    logger.info("切换模型：%s", args[0])
    return False


def _cmd_status(app: App, args: list[str]) -> bool:
    """一屏汇总：模型、权限模式、流式开关、当前会话、上下文占用、调用统计。"""
    console = app.renderer.console
    stats = app.session.stats()
    used = app.runtime.estimate_request_tokens()
    budget = app.runtime.compact_policy.max_context_tokens
    ratio = f"{used / budget * 100:.1f}%" if budget > 0 else "N/A"
    short = app.session.metadata.session_id[:8]
    title = app.session.metadata.title or "(无标题)"

    console.print("[bold]会话状态[/]")
    console.print(f"  模型         {app.settings.llm.model}")
    console.print(f"  权限模式     {MODE_LABELS[app.session.permissions.permission_mode]}")
    console.print(f"  流式输出     {'开' if app.settings.llm.stream else '关'}")
    console.print(f"  当前会话     {short}  {title}")
    console.print(f"  上下文占用   {used} / {budget} tokens（{ratio}）")
    console.print(
        f"  调用统计     模型 {stats['llm_calls']} 次 · 工具 {stats['tool_calls']} 次 ·"
        f" tokens 合计 {stats['total_tokens']}"
    )
    return False


def _cmd_todos(app: App, args: list[str]) -> bool:
    """展示当前任务清单（模型通过 todo_write 维护）；``clear`` 手动清空。"""
    console = app.renderer.console
    if args and args[0] == "clear" and len(args) == 1:
        app.session.set_todos([])
        console.print("[dim]任务清单已清空[/]")
        return False
    if args:
        console.print("[yellow]用法：/todos | /todos clear[/]")
        return False
    items = app.session.todos
    if not items:
        console.print("[dim]当前没有任务清单（模型规划多步任务时会用 todo_write 创建）[/]")
        return False
    for line in render_todo_lines([item.to_json() for item in items]):
        console.print(line)
    return False


def _cmd_changes(app: App, args: list[str]) -> bool:
    """列出本次会话里被写入/编辑过的文件——数据来自 ``session.workspace.modified_files``，
    由 ``write_file``/``edit_file`` 在落盘时标记（见 ``mark_modified``）。bash 里执行
    ``rm``/重定向等改动不在这份清单里：看不清目标文件是什么，宁可不追踪也不乱标。
    """
    from ..security.paths import display

    console = app.renderer.console
    paths = sorted(app.session.workspace.modified_files, key=str)
    if not paths:
        console.print("[dim]本次会话还没有通过 write_file/edit_file 修改过文件[/]")
        return False
    console.print(f"[bold]本次会话改动了 {len(paths)} 个文件：[/]")
    for path in paths:
        console.print(f"  [yellow]•[/] {display(app.settings.workspace, path)}")
    return False


def _cmd_compact(app: App, args: list[str]) -> bool:
    """不带参数立即触发一次压缩（原有行为）；``show``/``set <字段> <值>`` 查看或改
    ``runtime.compact_policy``（会话中途的副本，不动 ``AgentSettings.compact``）。
    """
    from ..session.compact import resolve_keep_from

    console = app.renderer.console
    if args and args[0] == "show" and len(args) == 1:
        _show_policy(console, "压缩策略（本会话）", app.runtime.compact_policy)
        return False
    if args and args[0] == "set":
        if len(args) != 3:
            console.print("[yellow]用法：/compact set <字段> <值>[/]")
            return False
        _set_policy_field(console, app.runtime.compact_policy, args[1], args[2])
        return False
    if args:
        console.print("[yellow]用法：/compact | /compact show | /compact set <字段> <值>[/]")
        return False

    keep_from = resolve_keep_from(
        app.session.messages, app.runtime.compact_policy.keep_recent_turns
    )
    if keep_from is None:
        app.renderer.notice("消息太少，无需压缩。", level="warning")
        return False
    app.renderer.notice("正在压缩较早的对话历史…")
    if app.runtime.compact(reason=COMPACT_REASON_MANUAL):
        app.renderer.notice("已压缩较早的对话历史。")
        logger.info("手动压缩会话历史")
    else:
        app.renderer.notice("压缩失败，对话历史未改动。", level="warning")
    return False


def _cmd_clear(app: App, args: list[str]) -> bool:
    session = app.start_new_session()
    short = session.metadata.session_id[:8]
    app.renderer.console.print(f"[dim]已开始新会话 {short}（旧日志仍保留）[/]")
    return False


def _cmd_new(app: App, args: list[str]) -> bool:
    return _cmd_clear(app, args)


def _cmd_history(app: App, args: list[str]) -> bool:
    from ..session.branch import user_event_entries

    entries = user_event_entries(app.session)
    if not entries:
        app.renderer.console.print("[dim]还没有用户消息[/]")
        return False
    for number, event in entries:
        preview = str(event.payload.get("text") or "").replace("\n", " ")[:60]
        short = event.id[:12]
        app.renderer.console.print(f"  [bold]{number}[/]  [dim]{short}[/]  {preview}")
    return False


def _cmd_sessions(app: App, args: list[str]) -> bool:
    from ..session.store import list_sessions, read_last

    home = app.session.store.home if app.session.store is not None else None
    items = list_sessions(app.settings.workspace, home=home)
    last = read_last(app.settings.workspace, home=home)
    if not items:
        app.renderer.console.print("[dim]这个工作区还没有已保存的会话[/]")
        return False
    current = app.session.metadata.session_id
    for item in items:
        mark = "●" if item.session_id == current else "○"
        last_mark = " last" if item.session_id == last else ""
        fork = f"  fork of {item.parent_id[:8]}" if item.parent_id else ""
        title = item.title or "(无标题)"
        app.renderer.console.print(
            f"  {mark} [bold]{item.session_id[:8]}[/]  {title}{fork}[dim]{last_mark}[/]"
        )
    return False


def _cmd_resume(app: App, args: list[str]) -> bool:
    """会话中途切到另一个会话：不带参数列出可选会话（同 /sessions），带 id/前缀直接切换。

    仿照 Claude Code 的 ``/resume``：切换后沿用**当前**会话的权限模式，不恢复目标
    会话磁盘上存的 ``permission_mode``——避免切完一个旧会话，权限模式突然变了。
    """
    from ..session.branch import load_session
    from ..session.store import list_sessions, resolve_session_id

    console = app.renderer.console
    home = app.session.store.home if app.session.store is not None else None

    if not args:
        items = list_sessions(app.settings.workspace, home=home)
        if not items:
            console.print("[dim]这个工作区还没有已保存的会话[/]")
            return False
        current = app.session.metadata.session_id
        for item in items:
            mark = "●" if item.session_id == current else "○"
            title = item.title or "(无标题)"
            console.print(f"  {mark} [bold]{item.session_id[:8]}[/]  {title}")
        console.print("[dim]/resume <id 或前缀> 切换到某个会话[/]")
        return False

    token = args[0]
    target_id = resolve_session_id(app.settings.workspace, token, home=home)
    if target_id is None:
        console.print(f"[yellow]找不到会话 {token}，或前缀不唯一；用 /resume 查看列表[/]")
        return False
    if target_id == app.session.metadata.session_id:
        console.print("[dim]已经是当前会话[/]")
        return False

    session, warning = load_session(app.settings.workspace, target_id, home=home)
    session.permissions.permission_mode = app.session.permissions.permission_mode
    session.persist_head()
    app.attach_session(session)
    if warning:
        console.print(f"[yellow]{warning}[/]")
    short = session.metadata.session_id[:8]
    title = session.metadata.title or "(无标题)"
    console.print(f"[dim]已切换到会话 {short}  {title}[/]")
    logger.info("切换会话：%s", session.metadata.session_id)
    return False


def _cmd_rewind(app: App, args: list[str]) -> bool:
    from ..session.branch import fork_at_user, resolve_user_anchor, view_index_for_user_event

    if not args:
        app.renderer.console.print("[yellow]用法：/rewind <序号或id> [restore|summarize-up|summarize-from][/]")
        return False
    anchor = resolve_user_anchor(app.session, args[0])
    if anchor is None:
        app.renderer.console.print("[yellow]找不到这条用户消息，先 /history 查看[/]")
        return False
    action = args[1].lower() if len(args) > 1 else ""
    if action not in {"restore", "summarize-up", "summarize-from"}:
        action = Prompt.ask(
            "选择操作",
            choices=["restore", "summarize-up", "summarize-from"],
            default="restore",
        )
    preview = str(anchor.payload.get("text") or "").replace("\n", " ")[:40]
    if action == "restore":
        session = fork_at_user(app.session, anchor.id)
        app.attach_session(session)
        app.renderer.console.print(
            f"[dim]已从「{preview}」分叉新会话 {session.metadata.session_id[:8]}。"
            "工作区文件保持现状，不会回滚。[/]"
        )
        logger.info("rewind restore fork=%s from=%s", session.metadata.session_id, anchor.id)
        return False

    index = view_index_for_user_event(app.session, anchor.id)
    if index is None:
        app.renderer.console.print(
            "[yellow]这条消息已不在当前上下文里（可能已被压缩）。请先 restore 再 summarize。[/]"
        )
        return False
    if action == "summarize-up":
        ok = app.runtime.compact(keep_from=index + 1, reason=COMPACT_REASON_SUMMARIZE_UP)
    else:
        last = len(app.session.messages) - 1
        ok = app.runtime.compact(
            keep_from=index, drop_end=last, reason=COMPACT_REASON_SUMMARIZE_FROM
        )
    if ok:
        app.renderer.notice("已按选择压缩这段历史（磁盘日志仍保留原文）。")
    else:
        app.renderer.notice("压缩失败，对话未改动。", level="warning")
    return False


def _cmd_cwd(app: App, args: list[str]) -> bool:
    app.renderer.console.print(f"  {app.settings.workspace}")
    return False


def _cmd_exit(app: App, args: list[str]) -> bool:
    app.renderer.console.print("[dim]再见[/]")
    logger.info("再见")
    return True


def build_default_commands() -> CommandRegistry:
    """注册默认斜杠命令。新增命令在此追加一行即可。"""
    registry = CommandRegistry()
    registry.register(SlashCommand("/help", "/help", "显示本帮助", _cmd_help))
    registry.register(
        SlashCommand(
            "/tools",
            "/tools [limits|<字段> <值>]",
            "列出已注册工具；limits 查看/修改工具限制",
            _cmd_tools,
        )
    )
    registry.register(
        SlashCommand("/mode", "/mode <模式>", "切换权限模式：plan / default / accept_edits", _cmd_mode)
    )
    registry.register(
        SlashCommand(
            "/permissions",
            "/permissions",
            "列出或增删权限规则（allow / ask / deny）",
            _cmd_permissions,
        )
    )
    registry.register(SlashCommand("/usage", "/usage", "显示本会话的 token 与调用统计", _cmd_usage))
    registry.register(SlashCommand("/stream", "/stream [on|off]", "查看或切换流式输出", _cmd_stream))
    registry.register(SlashCommand("/instructions", "/instructions", "列出已加载的指令层（cyan.md）", _cmd_instructions))
    registry.register(SlashCommand("/memory", "/memory", "列出项目自动记忆文件", _cmd_memory))
    registry.register(
        SlashCommand(
            "/compact",
            "/compact [show|set <字段> <值>]",
            "把较早的对话压缩成摘要；show/set 查看或修改压缩策略",
            _cmd_compact,
        )
    )
    registry.register(SlashCommand("/loop", "/loop [<字段> <值>]", "查看或修改循环限制（轮次上限等）", _cmd_loop))
    registry.register(SlashCommand("/context", "/context [<字段> <值>]", "查看或修改上下文截断策略", _cmd_context))
    registry.register(SlashCommand("/model", "/model [<名字>]", "查看或切换模型", _cmd_model))
    registry.register(SlashCommand("/status", "/status", "一屏汇总模型/权限/流式/上下文/统计", _cmd_status))
    registry.register(SlashCommand("/todos", "/todos [clear]", "查看当前任务清单（todo_write 维护）", _cmd_todos))
    registry.register(SlashCommand("/changes", "/changes", "列出本次会话改动过的文件", _cmd_changes))
    registry.register(SlashCommand("/history", "/history", "列出用户消息（完整日志）", _cmd_history))
    registry.register(
        SlashCommand(
            "/rewind",
            "/rewind <n>",
            "回退到某条用户消息：restore / summarize-up / summarize-from",
            _cmd_rewind,
        )
    )
    registry.register(SlashCommand("/sessions", "/sessions", "列出本工作区已保存的会话", _cmd_sessions))
    registry.register(
        SlashCommand(
            "/resume",
            "/resume [<id 或前缀>]",
            "切换到另一个会话（沿用当前权限模式）",
            _cmd_resume,
            aliases=("/continue",),
        )
    )
    registry.register(SlashCommand("/new", "/new", "开始新会话（旧日志保留）", _cmd_new))
    registry.register(SlashCommand("/clear", "/clear", "同 /new，开始新会话", _cmd_clear))
    registry.register(SlashCommand("/cwd", "/cwd", "显示当前工作目录", _cmd_cwd))
    registry.register(SlashCommand("/exit", "/exit", "退出", _cmd_exit, aliases=("/quit",)))
    return registry
