"""终端渲染与审批交互。

用户看到的是 rich Console；同一条事件再写入 logging 文件，方便事后复盘。
两者职责分开：Console 管观感，logging 管落盘。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from rich import box
from rich.cells import cell_len
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
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
# 审批面板边框按能力上色：exec 风险最高用红，write 居中用黄，read 很少走到这里用青，
# 跟 banner/工具起始行共用的青色主题呼应，一眼能分出「这次要批准的是什么级别的操作」。
_CAPABILITY_BORDER_COLOR = {
    "read": "cyan",
    "write": "yellow",
    "exec": "red",
}
_DECISION_BY_KEY = {
    "y": ApprovalDecision.ALLOW_ONCE,
    "n": ApprovalDecision.DENY,
    "a": ApprovalDecision.ALLOW_ALWAYS,
}
_BANNER_WIDTH = 64  # 启动横幅固定宽度，不随终端宽度变化（终端比这个还窄时才会被迫收窄）
_LIVE_MIN_INTERVAL = 0.08  # 秒；避免每个分片都重新解析整段内容造成 CPU 抖动
# 这几个工具的哪个参数值得实时预览：write_file 边生成边看新内容，
# edit_file 边生成边看替换后的新文本——跟 Claude Code 靠 fine-grained tool
# streaming 展示 Write/Edit 内容"typing"出来的效果一样。
_TOOL_PREVIEW_FIELD = {
    "write_file": "content",
    "edit_file": "new_string",
}


@dataclass
class _ToolPreviewState:
    """一次工具调用参数流式拼装过程中的缓冲区，只用于渲染，不参与真正执行。"""

    index: int
    name: str | None = None
    call_id: str | None = None
    arguments: str = ""


class Renderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._live: Live | None = None
        self._live_kind: str | None = None  # "text" | "tool"
        self._live_buffer: str = ""
        self._live_last_update: float = 0.0
        self._tool_preview: _ToolPreviewState | None = None

    # ------------------------------------------------------------------ 通用
    def banner(
        self,
        settings: AgentSettings,
        tool_names: list[str],
        permission_mode: PermissionMode,
        instruction_labels: list[str] | None = None,
    ) -> None:
        """启动横幅：用 grid 对齐键值对，标题/提示挪到 Panel 的 title/subtitle 上，
        不再跟正文混在一起当第一/最后一行——留白更清爽，字段名对不齐的问题也顺带解决了
        （grid 按最宽的那列自动对齐，不用手动拼空格）。

        宽度定死成 ``_BANNER_WIDTH``，不随终端宽度变化：光靠 ``expand=False`` 不够——
        那只是「不主动撑满终端」，遇到终端比内容还窄（比如工作目录路径很长）时还是
        会被 clamp 到终端宽度，看起来就像跟着终端在变。第二列加
        ``no_wrap`` + ``overflow="ellipsis"``，长路径会被截断成省略号，而不是把
        box 撑宽；第一列显式给 ``width``（按实际标签宽度算，中文按 2 算），不然
        总宽度不够时 Table 会连着标签列一起等比例缩，短标签也会被截断。
        """
        rows = [
            ("模型", f"[green]{settings.llm.model}[/]"),
            ("权限模式", f"[green]{MODE_LABELS[permission_mode]}[/]"),
            ("工作目录", f"[dim]{settings.workspace}[/]"),
        ]
        if instruction_labels:
            rows.append(("指令层", f"[dim]{' · '.join(instruction_labels)}[/]"))
        label_width = max(cell_len(label) for label, _ in rows)

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold", no_wrap=True, width=label_width)
        grid.add_column(overflow="ellipsis")
        for label, value in rows:
            grid.add_row(label, value)

        self.console.print(
            Panel(
                grid,
                title="[bold cyan]Cyan[/]",
                title_align="left",
                subtitle="[dim]/help 查看命令 · Ctrl-C 中断当前任务[/]",
                subtitle_align="left",
                border_style="cyan",
                box=box.HEAVY,
                padding=(1, 2),
                width=min(_BANNER_WIDTH, self.console.width),
            )
        )
        logger.info("启动 模型=%s workspace=%s tools=%s", settings.llm.model, settings.workspace, ", ".join(tool_names))

    def notice(self, message: str, level: str = "info") -> None:
        style = {"error": "bold red", "warning": "yellow", "info": "dim"}.get(level, "dim")
        self.console.print(f"[{style}]{message}[/]")
        getattr(logger, level if level in {"error", "warning", "info"} else "info")("%s", message)

    def error(self, message: str) -> None:
        self.console.print(f"[bold red]错误[/] {message}")
        logger.error("%s", message)

    def assistant(self, text: str) -> None:
        """一轮文本的定稿输出；若正在流式展示，先收尾再打印完整 Markdown。"""
        self.stop_live_preview()
        self.console.print(Markdown(text))
        self.console.print()
        logger.info("助手：\n%s", text)

    def assistant_delta(self, text: str) -> None:
        """流式增量：懒启动一个 transient 的 ``Live`` 面板，边收边刷新打字机效果。

        ``transient=True`` 让中间态渲染不残留在终端滚动历史里；
        真正写入历史的是 ``assistant()`` 最后那次完整 Markdown 打印。
        增量本身立即累积进缓冲区，但重新解析 Markdown 并刷新终端按
        ``_LIVE_MIN_INTERVAL`` 节流——模型吐字很快时不必每个分片都重绘一次。

        同一个 ``Live`` 区域也被工具参数预览（见 ``tool_call_delta``）复用；
        如果上一刻还在展示工具预览，这里先收尾切换过去。
        """
        if self._live is not None and self._live_kind != "text":
            self.stop_live_preview()
        if self._live is None:
            self._live = Live(console=self.console, refresh_per_second=12, transient=True)
            self._live.start()
            self._live_kind = "text"
            self._live_buffer = ""
            self._live_last_update = 0.0
        self._live_buffer += text
        now = time.monotonic()
        if now - self._live_last_update >= _LIVE_MIN_INTERVAL:
            self._live.update(Markdown(self._live_buffer))
            self._live_last_update = now

    def tool_call_delta(self, index: int, call_id: str | None, name: str | None, arguments_delta: str) -> None:
        """工具调用参数 JSON 的一次流式分片：实时预览正在拼装的内容。

        write_file/edit_file 会尽力从还没解析完的 JSON 里抠出 ``content``/
        ``new_string`` 字段，边生成边展示；其它工具退化成展示原始 JSON 片段。
        真正执行仍然等完整 JSON 拼好、由 ``ToolStarted`` 之前的
        ``stop_live_preview()`` 收尾。

        协议上（Anthropic/OpenAI 兼容都一样）一次只会有一个 tool_call 在吐分片，
        下一个 tool_call 开始吐之前，上一个必然已经吐完——所以 index 变化就意味着
        上一个工具的参数已经拼好了，直接把它定格成一条静态记录留在回滚历史里，
        而不是无声消失，视觉上更接近"一个工具卡片接一个出现"的效果。
        """
        if self._live is not None and self._live_kind == "tool" and self._tool_preview is not None and self._tool_preview.index != index:
            self._freeze_tool_preview()
        elif self._live is not None and self._live_kind != "tool":
            self.stop_live_preview()

        if self._tool_preview is None or self._tool_preview.index != index:
            self._tool_preview = _ToolPreviewState(index=index)
        state = self._tool_preview
        if name:
            state.name = (state.name or "") + name
        if call_id:
            state.call_id = call_id
        state.arguments += arguments_delta

        if self._live is None:
            self._live = Live(console=self.console, refresh_per_second=12, transient=True)
            self._live.start()
            self._live_kind = "tool"
            self._live_last_update = 0.0
        now = time.monotonic()
        if now - self._live_last_update >= _LIVE_MIN_INTERVAL:
            self._live.update(_tool_preview_panel(state))
            self._live_last_update = now

    def _freeze_tool_preview(self) -> None:
        """把当前工具的预览从 ``Live`` 里定格成一条静态记录，留在回滚历史里。

        用于模型在一轮里连续发起多个 tool_call 时——切到下一个 index 前，
        上一个工具的参数已经拼完，不该无声消失。真正的执行结果
        （``ToolStarted``/``ToolFinished``）会在执行阶段照常打印，
        这条记录只是"参数已生成"的定格快照，两者不冲突。
        """
        if self._tool_preview is not None:
            self.console.print(_tool_preview_panel(self._tool_preview, finalized=True))
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._live_kind = None
        self._live_buffer = ""
        self._live_last_update = 0.0
        self._tool_preview = None

    def abort_live(self) -> None:
        """Ctrl-C 中断落在流式输出中途时，避免残留一个没关掉的 Live。"""
        self.stop_live_preview()

    def stop_live_preview(self) -> None:
        """收尾当前活跃的流式预览（文本或工具参数），转入下一阶段渲染前调用。"""
        if self._live is not None:
            if self._live_kind == "text":
                # 节流可能让最后几个分片还没刷到屏幕上，收尾前补一次，避免看起来被截断。
                self._live.update(Markdown(self._live_buffer))
            self._live.stop()
            self._live = None
        self._live_kind = None
        self._live_buffer = ""
        self._live_last_update = 0.0
        self._tool_preview = None

    def thinking(self, iteration: int) -> None:
        self.console.print(f"[dim italic]· 第 {iteration} 轮，思考中...[/]")
        logger.info("第 %s 轮，思考中", iteration)

    def waiting_spinner(self) -> Any:
        """等待模型返回下一个事件期间的转圈动画。

        只是个简单的 spinner，没有状态词轮换也没有计时——``Console.status()``
        自带独立的刷新线程，只负责重绘动画帧，包住的那次调用仍在主线程同步执行，
        不影响 Ctrl-C 中断的现有语义。
        """
        return self.console.status("[dim]等待模型响应…[/]", spinner="dots")

    # ------------------------------------------------------------------ 工具
    def tool_started(self, name: str, args: dict[str, Any]) -> None:
        self.stop_live_preview()
        self.console.print(f"[bold cyan]▸ {name}[/] [dim]{_format_args(name, args)}[/]")
        logger.info("调用 %s  %s", name, _format_args(name, args))
        logger.debug("工具参数 %s: %s", name, json.dumps(args, ensure_ascii=False, default=str))

    def tool_finished(self, name: str, result: ToolRunResult, duration: float) -> None:
        """一行摘要；bash 额外摘几行输出，写文件则附上 diff，todo_write 则展开清单。"""
        text = result.content if result.ok else (result.error or "执行失败")
        command_failed = result.metadata.get("exit_code") not in (None, 0)
        mark = "[green]✓[/]" if result.ok and not command_failed else "[red]✗[/]"

        if name == "todo_write" and result.ok and "todos" in result.metadata:
            summary = "任务清单已更新" if result.metadata["todos"] else "任务清单已清空"
            self.console.print(f"  {mark} {summary} [dim]({duration:.1f}s)[/]")
            logger.info("%s 成功  %s  (%.1fs)", name, summary, duration)
            for line in render_todo_lines(result.metadata["todos"]):
                self.console.print(Text.from_markup(line))
            self.console.print()
            return

        self.console.print(f"  {mark} {_first_line(text)} [dim]({duration:.1f}s)[/]")
        logger.info("%s %s  %s  (%.1fs)", name, "成功" if result.ok else "失败", _first_line(text), duration)
        if not result.ok:
            logger.warning("%s 失败详情：%s", name, result.error)
        logger.debug("%s 完整输出：\n%s", name, text)

        if "exit_code" in result.metadata:
            for line in _output_excerpt(text):
                self.console.print(f"    [dim]{line}[/]")

        if name == "read_file" and result.metadata.get("preview"):
            preview = result.metadata["preview"]
            preview_start = int(result.metadata.get("preview_start", 1))
            lexer = Syntax.guess_lexer(str(result.metadata.get("path") or ""), code=preview)
            self.console.print(
                Syntax(
                    preview,
                    lexer,
                    theme="ansi_dark",
                    background_color="default",
                    line_numbers=True,
                    start_line=preview_start,
                )
            )
            last_previewed_line = preview_start - 1 + len(preview.splitlines())
            if int(result.metadata.get("total_lines", 0)) > last_previewed_line:
                self.console.print("  [dim]...[/]")
            self.console.print()

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
        border_style = _CAPABILITY_BORDER_COLOR.get(request.capability, "yellow")
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
                border_style=border_style,
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
    def task_finished(self, reason: StopReason, stats: dict[str, Any], elapsed: float | None = None) -> None:
        """任务收尾用一张卡片而不是一行灰字——录屏/演示时这是最后一帧画面，
        值得比"运行中"的普通行更醒目一点。正常结束绿色打勾，异常终止黄色叹号
        （不用 ``✗``，那个符号已经被工具失败占用了，这里不是失败，是提前停）。
        """
        parts = [
            f"[bold]{stats.get('llm_calls', 0)}[/] 次模型调用",
            f"[bold]{stats.get('tool_calls', 0)}[/] 次工具调用",
            f"[bold]{stats.get('total_tokens', 0)}[/] tokens",
        ]
        if elapsed is not None:
            parts.append(f"用时 [bold]{_format_duration(elapsed)}[/]")
        summary = " · ".join(parts)
        plain_summary = summary.replace("[bold]", "").replace("[/]", "")

        if reason is StopReason.COMPLETED:
            title = "[bold green]✓ 任务完成[/]"
            border_style = "green"
            logger.info("%s", plain_summary)
        else:
            title = f"[bold yellow]! {STOP_REASON_TEXT[reason]}[/]"
            border_style = "yellow"
            logger.warning("%s（%s）", STOP_REASON_TEXT[reason], plain_summary)

        self.console.print(
            Panel(
                Text.from_markup(summary),
                title=title,
                title_align="left",
                border_style=border_style,
                box=box.ROUNDED,
                padding=(0, 2),
                expand=False,
            )
        )
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


def _tool_preview_panel(state: _ToolPreviewState, *, finalized: bool = False) -> Any:
    """把正在流式拼装的工具参数渲成一个 Panel：write_file/edit_file 显示内容本身，其它工具退化成原始 JSON 片段。

    ``finalized=True`` 用于定格快照（见 ``Renderer._freeze_tool_preview``）——
    换个标记和边框颜色，跟还在实时刷新的预览区分开。
    """
    name = state.name or "..."
    path = extract_partial_string_field(state.arguments, "path")
    marker = "✓" if finalized else "▸"
    title = f"{marker} {name}{f' {path}' if path else ''}"

    field = _TOOL_PREVIEW_FIELD.get(state.name or "")
    body: Any
    if field is not None:
        preview = extract_partial_string_field(state.arguments, field)
        if preview is None:
            body = Text("生成参数中...", style="dim")
        elif preview == "":
            body = Text("(内容为空)", style="dim")
        else:
            body = Syntax(preview, "text", theme="ansi_dark", background_color="default", word_wrap=True)
    else:
        body = Text(_clip(state.arguments, 300) or "...", style="dim")
    return Panel(body, title=title, border_style="dim" if finalized else "cyan", padding=(0, 1))


def extract_partial_string_field(partial_json: str, field_name: str) -> str | None:
    """从还没解析完的 JSON 片段里尽力抠出某个字符串字段目前已经流到的内容。

    只用于 CLI 实时预览——真正执行工具要等完整 JSON 拼好，走
    ``parse_tool_arguments`` 正经解析。找不到该字段（还没流到）返回 ``None``；
    字段本身还在流式生成时，返回已经解码出来的部分，遇到还没流完的转义序列
    （比如 ``\\`` 是最后一个字符，或者 ``\\uXXXX`` 还没写满 4 位）就停在那里，
    等下一次分片来了再继续，不会展示半个转义字符。
    """
    marker = f'"{field_name}"'
    marker_at = partial_json.find(marker)
    if marker_at == -1:
        return None
    cursor = marker_at + len(marker)
    length = len(partial_json)

    while cursor < length and partial_json[cursor] in " \t\r\n":
        cursor += 1
    if cursor >= length or partial_json[cursor] != ":":
        return None
    cursor += 1
    while cursor < length and partial_json[cursor] in " \t\r\n":
        cursor += 1
    if cursor >= length or partial_json[cursor] != '"':
        return None
    cursor += 1

    simple_escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
    decoded: list[str] = []
    while cursor < length:
        char = partial_json[cursor]
        if char == '"':
            break
        if char != "\\":
            decoded.append(char)
            cursor += 1
            continue
        if cursor + 1 >= length:
            break  # 转义序列还没流完，先不展示这半个字符，等下一片
        escape = partial_json[cursor + 1]
        if escape in simple_escapes:
            decoded.append(simple_escapes[escape])
            cursor += 2
            continue
        if escape == "u":
            if cursor + 6 > length:
                break  # \uXXXX 还没流完
            try:
                decoded.append(chr(int(partial_json[cursor + 2 : cursor + 6], 16)))
            except ValueError:
                break
            cursor += 6
            continue
        break  # 未知转义，保守地当成还没流完
    return "".join(decoded)


def _format_args(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "bash":
        return _clip(str(args.get("command", "")), 100)
    if tool_name == "todo_write":
        items = args.get("todos") or []
        in_progress = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "in_progress")
        done = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "completed")
        return f"{len(items)} 项，{done} 完成，{in_progress} 进行中"
    if "path" in args:
        extra = ""
        if tool_name == "list_dir":
            extra = f" depth={args.get('depth', 2)}"
        return f"{args['path']}{extra}"
    return _clip(json.dumps(args, ensure_ascii=False), 100)


_TODO_STATUS_GLYPH = {
    "completed": "[green]✓[/]",
    "in_progress": "[yellow]●[/]",
    "pending": "[dim]○[/]",
}


def render_todo_lines(items: list[dict[str, Any]]) -> list[str]:
    """把任务清单渲成带勾选状态的 rich markup 行，``tool_finished`` 和 ``/todos`` 共用。

    输入是 ``TodoItem.to_json()`` 的形状（``content`` / ``status`` / ``active_form``），
    不直接依赖 ``session.types``——渲染层只认字典，不认领域对象。
    """
    lines: list[str] = []
    for item in items:
        status = str(item.get("status") or "pending")
        content = str(item.get("content") or "")
        active_form = str(item.get("active_form") or item.get("activeForm") or "")
        glyph = _TODO_STATUS_GLYPH.get(status, _TODO_STATUS_GLYPH["pending"])
        if status == "completed":
            body = f"[dim strike]{content}[/]"
        elif status == "in_progress":
            body = f"[bold yellow]{active_form or content}[/]"
        else:
            body = content
        lines.append(f"  {glyph} {body}")
    return lines


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


def _format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}分{rest}秒"
