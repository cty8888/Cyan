"""终端渲染与审批交互。

用户看到的是 rich Console；同一条事件再写入 logging 文件，方便事后复盘。
两者职责分开：Console 管观感，logging 管落盘。
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.text import Text

from ..core.types import StopReason
from ..logutil import get_logger
from ..security.types import ApprovalDecision, ApprovalRequest, PermissionMode
from ..settings import AgentSettings
from ..tools.types import ToolRunResult

logger = get_logger("cli")

MODE_LABELS = {
    PermissionMode.PLAN: "Plan (只读规划)",
    PermissionMode.DEFAULT: "Default (默认)",
    PermissionMode.ACCEPT_EDITS: "AcceptEdits (自动批准编辑)",
}
STOP_REASON_TEXT = {
    StopReason.COMPLETED: "任务结束",
    StopReason.MAX_ITERATIONS: "达到最大轮次上限，已停止",
    StopReason.TOOL_FAILURES: "连续多次工具调用失败，已停止",
    StopReason.REPEATED_CALLS: "检测到重复的无效调用，已停止",
    StopReason.USER_ABORT: "已被用户中断",
    StopReason.FATAL_ERROR: "发生不可恢复的错误，已停止",
}

_CAPABILITY_LABEL = {
    "read": "只读",
    "write": "写入",
    "exec": "执行",
}
_DECISION_BY_KEY = {
    "y": ApprovalDecision.ALLOW_ONCE,
    "n": ApprovalDecision.DENY,
    "a": ApprovalDecision.ALLOW_ALWAYS,
}


class Renderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    # ------------------------------------------------------------------ 通用
    def banner(
        self,
        settings: AgentSettings,
        tool_names: list[str],
        permission_mode: PermissionMode,
        instruction_labels: list[str] | None = None,
    ) -> None:
        lines = [
            Text.from_markup(f"[bold cyan]Cyan[/]  模型 [green]{settings.llm.model}[/]"),
            Text.from_markup(
                f"权限模式  [green]{MODE_LABELS[permission_mode]}[/]"
            ),
            Text.from_markup(f"工作目录  [dim]{settings.workspace}[/]"),
            Text.from_markup(f"可用工具  [dim]{', '.join(tool_names)}[/]"),
            Text.from_markup(f"日志文件  [dim]{settings.log_dir / 'agent.log'}[/]"),
        ]
        if instruction_labels:
            lines.append(
                Text.from_markup(f"指令层  [dim]{' · '.join(instruction_labels)}[/]")
            )
        lines.append(Text.from_markup("[dim]输入任务开始，/help 查看命令，Ctrl-C 中断当前任务[/]"))
        self.console.print(Panel(Text("\n").join(lines), border_style="cyan", padding=(0, 1)))
        logger.info("启动 模型=%s workspace=%s tools=%s", settings.llm.model, settings.workspace, ", ".join(tool_names))

    def notice(self, message: str, level: str = "info") -> None:
        style = {"error": "bold red", "warning": "yellow", "info": "dim"}.get(level, "dim")
        self.console.print(f"[{style}]{message}[/]")
        getattr(logger, level if level in {"error", "warning", "info"} else "info")("%s", message)

    def error(self, message: str) -> None:
        self.console.print(f"[bold red]错误[/] {message}")
        logger.error("%s", message)

    def assistant(self, text: str) -> None:
        self.console.print(Markdown(text))
        self.console.print()
        logger.info("助手：\n%s", text)

    def thinking(self, iteration: int) -> None:
        self.console.print(f"[dim]· 第 {iteration} 轮，思考中...[/]")
        logger.info("第 %s 轮，思考中", iteration)

    # ------------------------------------------------------------------ 工具
    def tool_started(self, name: str, args: dict[str, Any]) -> None:
        self.console.print(f"[bold blue]▸ {name}[/] [dim]{_format_args(name, args)}[/]")
        logger.info("调用 %s  %s", name, _format_args(name, args))
        logger.debug("工具参数 %s: %s", name, json.dumps(args, ensure_ascii=False, default=str))

    def tool_finished(self, name: str, result: ToolRunResult, duration: float) -> None:
        """一行摘要；bash 额外摘几行输出，写文件则附上 diff。"""
        text = result.content if result.ok else (result.error or "执行失败")
        command_failed = result.metadata.get("exit_code") not in (None, 0)
        mark = "[green]✓[/]" if result.ok and not command_failed else "[red]✗[/]"
        self.console.print(f"  {mark} {_first_line(text)} [dim]({duration:.1f}s)[/]")
        logger.info("%s %s  %s  (%.1fs)", name, "成功" if result.ok else "失败", _first_line(text), duration)
        if not result.ok:
            logger.warning("%s 失败详情：%s", name, result.error)
        logger.debug("%s 完整输出：\n%s", name, text)

        if "exit_code" in result.metadata:
            for line in _output_excerpt(text):
                self.console.print(f"    [dim]{line}[/]")

        diff = result.metadata.get("diff")
        if diff and diff != "(无变化)":
            self.console.print(Syntax(diff, "diff", theme="ansi_dark", background_color="default"))
            logger.info("diff:\n%s", diff)
        self.console.print()

    # ------------------------------------------------------------------ 审批
    def ask_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """弹出审批面板，读取 y/n/a，返回对应的 ``ApprovalDecision``。

        ``force=True`` 时没有「始终允许」。EOF 视为拒绝；Ctrl-C 向上抛，由 CLI 中断整次任务。
        """
        capability = _CAPABILITY_LABEL.get(request.capability, request.capability)
        title = f"需要确认 · {capability}"
        body: list[Any] = [Text.from_markup(f"[bold]{request.summary}[/]")]

        if request.detail:
            body.append(Text())
            body.append(_render_detail(request.detail, request.detail_format))
        if request.reason:
            body.append(Text())
            body.append(Text.from_markup(f"[yellow]注意：{request.reason}[/]"))

        self.console.print(
            Panel(
                _stack(body),
                title=title,
                border_style="yellow",
                padding=(0, 1),
            )
        )
        logger.info("%s  %s", title, request.summary)
        if request.detail:
            logger.info("详情:\n%s", request.detail)

        can_always = not request.force and bool(request.always_label)
        choices = ["y", "n", "a"] if can_always else ["y", "n"]
        hint = "y=允许  n=拒绝"
        if can_always:
            hint += f"  a=本会话始终允许 {request.always_label}"
        self.console.print(f"[dim]{hint}[/]")

        try:
            answer = Prompt.ask("是否执行", choices=choices, default="n", console=self.console)
        except EOFError:
            self.console.print("\n[yellow]已取消[/]\n")
            logger.warning("审批已取消")
            return ApprovalDecision.DENY
        except KeyboardInterrupt:
            self.console.print("\n[yellow]已中断[/]\n")
            logger.warning("审批时用户中断任务")
            raise
        self.console.print()
        decision = _DECISION_BY_KEY.get(answer, ApprovalDecision.DENY)
        logger.info("审批结果：%s", decision.value)
        return decision

    # ------------------------------------------------------------------ 收尾
    def task_finished(self, reason: StopReason, stats: dict[str, Any]) -> None:
        summary = (
            f"{stats.get('llm_calls', 0)} 次模型调用 · "
            f"{stats.get('tool_calls', 0)} 次工具调用 · "
            f"{stats.get('total_tokens', 0)} tokens"
        )
        if reason is StopReason.COMPLETED:
            self.console.print(f"[dim]{summary}[/]")
            logger.info("%s", summary)
        else:
            self.console.print(f"[yellow]{STOP_REASON_TEXT[reason]}[/] [dim]（{summary}）[/]")
            logger.warning("%s（%s）", STOP_REASON_TEXT[reason], summary)
        self.console.print()


def _stack(items: list[Any]) -> Any:
    from rich.console import Group

    return Group(*items)


def _render_detail(detail: str, fmt: str) -> Any:
    """按 detail_format 选语法高亮：diff / shell / 纯文本。"""
    if fmt == "diff":
        return Syntax(detail, "diff", theme="ansi_dark", background_color="default")
    if fmt in {"shell", "bash"}:
        return Syntax(detail, "bash", theme="ansi_dark", background_color="default", word_wrap=True)
    if fmt == "text":
        return Text(detail)
    return Syntax(detail, fmt, theme="ansi_dark", background_color="default")


def _format_args(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "bash":
        return _clip(str(args.get("command", "")), 100)
    if "path" in args:
        extra = ""
        if tool_name == "list_dir":
            extra = f" depth={args.get('depth', 2)}"
        return f"{args['path']}{extra}"
    return _clip(json.dumps(args, ensure_ascii=False), 100)


def _first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()
    return _clip(line[0], limit) if line else ""


def _output_excerpt(text: str, max_lines: int = 8) -> list[str]:
    # 内容格式是「头部说明行 + 空行 + 实际输出」，头部行数不固定（退出码/目录/超时提示/cwd 提示），
    # 所以按第一个空行切开，而不是硬编码跳过第一行
    _, _, tail = (text or "").partition("\n\n")
    lines = [line for line in tail.splitlines() if line.strip() and line.strip() != "(无输出)"]
    excerpt = [_clip(line, 140) for line in lines[:max_lines]]
    if len(lines) > max_lines:
        excerpt.append(f"... 另有 {len(lines) - max_lines} 行输出")
    return excerpt


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
