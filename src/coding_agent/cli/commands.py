"""交互式 REPL 的斜杠命令。

设计上与 ``tools/registry.py`` 保持一致：新增一个命令 = 写一个 handler 函数 + 在
``build_default_commands()`` 里注册一行，不需要再去改一条 if/elif 链，``/help`` 的
文本也会跟着自动列出新命令，不需要手动同步一份 ``HELP_TEXT`` 字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterator

from ..logutil import get_logger
from ..security.types import PermissionMode
from .renderer import MODE_LABELS

if TYPE_CHECKING:
    from .app import App

logger = get_logger("cli")

# handler 返回 True 表示应当退出 REPL。
CommandHandler = Callable[["App", list[str]], bool]


@dataclass(frozen=True)
class SlashCommand:
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
    for tool in app.registry:
        app.renderer.console.print(
            f"  [bold]{tool.name}[/] "
            f"[dim]({tool.capability.value}/{tool.risk.value})[/] — {tool.description}"
        )
    return False


def _cmd_mode(app: App, args: list[str]) -> bool:
    console = app.renderer.console
    if len(args) != 1:
        console.print("[yellow]用法：/mode plan|default|accept_edits|bypass[/]")
        return False
    try:
        mode = PermissionMode(args[0])
    except ValueError:
        console.print("[yellow]无效权限模式, 可选: plan / default / accept_edits / bypass[/]")
        return False
    app.session.permissions.permission_mode = mode
    console.print(f"[dim]已切换至 {MODE_LABELS[mode]}[/]")
    logger.info("切换权限模式：%s", mode.value)
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


def _cmd_clear(app: App, args: list[str]) -> bool:
    app.session.clear()
    app.renderer.console.print("[dim]已清空对话历史[/]")
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
    registry.register(SlashCommand("/tools", "/tools", "列出已注册的工具", _cmd_tools))
    registry.register(
        SlashCommand("/mode", "/mode <模式>", "切换权限模式：plan / default / accept_edits / bypass", _cmd_mode)
    )
    registry.register(SlashCommand("/usage", "/usage", "显示本会话的 token 与调用统计", _cmd_usage))
    registry.register(SlashCommand("/clear", "/clear", "清空对话历史，开始新会话", _cmd_clear))
    registry.register(SlashCommand("/cwd", "/cwd", "显示当前工作目录", _cmd_cwd))
    registry.register(SlashCommand("/exit", "/exit", "退出", _cmd_exit, aliases=("/quit",)))
    return registry
