"""bash —— 项目内唯一的 shell 执行入口。

每次调用启动独立进程；工作目录在会话内延续，环境变量不保留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...constants.tools.defs.bash import BASH_CWD_MARKER, BASH_DESCRIPTION, BASH_NAME, BASH_PARAMETERS
from ...security.paths import display
from .._process import run_process
from ..base import RiskLevel, Tool, ToolCapability, ToolContext, ToolRunResult


class BashTool(Tool):
    name = BASH_NAME
    description = BASH_DESCRIPTION
    capability = ToolCapability.EXEC
    risk = RiskLevel.HIGH
    parameters = BASH_PARAMETERS

    def describe(self, args: dict[str, Any], workspace: Path) -> tuple[str, str | None, str]:
        return "执行命令", str(args.get("command", "")), "shell"

    def run(self, ctx: ToolContext, command: str, timeout_ms: int = 120_000) -> ToolRunResult:
        cwd = ctx.session.bash_cwd or ctx.workspace
        if not cwd.is_dir():
            # 上次记录的目录已不存在，退回工作目录根
            cwd = ctx.workspace
            ctx.session.bash_cwd = None

        timeout_seconds = max(0.001, int(timeout_ms) / 1000)
        wrapped = command + _state_trailer()

        result = run_process(["bash", "-c", wrapped], cwd, timeout_seconds, merge_stderr=True)

        visible, cwd_text = _extract_cwd(result.stdout)
        cwd_note = self._update_cwd(ctx, cwd_text)

        output = _truncate_tail(visible.strip(), ctx.tool_config.max_tool_output_chars) or "(无输出)"
        display_cwd = display(ctx.workspace, ctx.session.bash_cwd or ctx.workspace)

        lines = [f"退出码：{result.exit_code}", f"目录：{display_cwd}"]
        if result.timed_out:
            lines.append(f"执行超时（超过 {timeout_ms}ms），进程已被终止。")
        if cwd_note:
            lines.append(cwd_note)
        lines.append(f"\n{output}")
        content = "\n".join(lines)

        metadata = {"exit_code": result.exit_code, "cwd": display_cwd}
        if result.timed_out or result.exit_code != 0:
            return ToolRunResult(ok=False, error=content, metadata=metadata)
        return ToolRunResult.success(content, **metadata)

    def _update_cwd(self, ctx: ToolContext, cwd_text: str | None) -> str | None:
        """解析命令结束后的目录，更新会话 cwd；越界时重置并返回提示。"""
        if not cwd_text:
            return None
        try:
            new_cwd = Path(cwd_text).resolve()
        except OSError:
            return None
        if new_cwd == ctx.workspace or ctx.workspace in new_cwd.parents:
            ctx.session.bash_cwd = new_cwd
            return None
        ctx.session.bash_cwd = None
        return (
            f"注意：命令结束后所在目录越出了工作区，已重置回工作目录根 "
            f"{display(ctx.workspace, ctx.workspace)}"
        )


def _truncate_tail(text: str, limit: int) -> str:
    """保留开头 limit 个字符，超出部分截断。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _state_trailer() -> str:
    """追加在用户命令之后：保留退出码，并打印命令结束时的 $PWD。"""
    return f'\n__ca_exit=$?\nprintf "\\n{BASH_CWD_MARKER}%s\\n" "$PWD"\nexit "$__ca_exit"\n'


def _extract_cwd(output: str) -> tuple[str, str | None]:
    """从合并输出中剥离 cwd 标记行，返回 (可见内容, 命令结束目录)。"""
    idx = output.rfind(BASH_CWD_MARKER)
    if idx == -1:
        return output, None
    visible = output[:idx]
    remainder = output[idx + len(BASH_CWD_MARKER) :]
    cwd_line = remainder.splitlines()[0] if remainder.splitlines() else ""
    return visible, cwd_line.strip() or None
