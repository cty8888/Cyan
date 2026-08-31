"""bash —— 项目内唯一的 shell 执行入口。

每次调用启动独立进程；工作目录在会话内延续，环境变量不保留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...errors import BlockedCommandError, SecurityError
from ...security.command_paths import analyze_command, reject_unsafe_paths, written_paths
from ...security.paths import display
from ...security.rules import blocked_command, restricted_command
from ..base import Tool
from ..process import run_process
from ..types import ToolCapability, ToolContext, ToolRunResult

BASH_NAME = "bash"
BASH_DESCRIPTION = (
    "在项目工作目录下执行 shell 命令 (测试, 构建, git, 脚本等). "
    "每条命令都在独立的新进程里运行; 工作目录会在调用之间延续——"
    "命令里执行了 cd 并且最终停在工作目录内, 下一次调用会从那个目录继续, "
    "越出工作目录会被重置回工作目录根. 不会保留环境变量或 shell 别名, "
    "命令里的 export 不会影响下一次调用."
)
BASH_DEFAULT_TIMEOUT_MS = 120_000
BASH_CWD_MARKER = "@@CYAN_CWD@@"
BASH_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令"},
        "timeout_ms": {
            "type": "integer",
            "description": "超时毫秒数, 默认 120000 (120 秒).",
            "default": BASH_DEFAULT_TIMEOUT_MS,
        },
    },
    "required": ["command"],
}


class BashTool(Tool):
    name = BASH_NAME
    description = BASH_DESCRIPTION
    capability = ToolCapability.EXEC
    parameters = BASH_PARAMETERS

    def describe(self, args: dict[str, Any], workspace: Path, workspace_access=None) -> tuple[str, str | None, str]:
        """审批面板：摘要固定为「执行命令」，详情是待执行的 shell 原文。"""
        return "执行命令", str(args.get("command", "")), "shell"

    def run(self, ctx: ToolContext, command: str, timeout_ms: int = BASH_DEFAULT_TIMEOUT_MS) -> ToolRunResult:
        # 权限层已经拦过一遍；这里再拦一次，避免有人绕过 PermissionManager 直接 execute。
        cwd = ctx.workspace_access.bash_cwd or ctx.workspace
        if not cwd.is_dir():
            # 上次记录的目录已不存在，退回工作目录根
            cwd = ctx.workspace
            ctx.workspace_access.bash_cwd = None
        blocked = blocked_command(command, workspace=ctx.workspace, cwd=cwd)
        if blocked:
            raise BlockedCommandError(blocked)
        restricted = restricted_command(command)
        if restricted:
            raise SecurityError(restricted)
        reject_unsafe_paths(ctx.workspace, command, cwd)

        timeout_ms = min(max(1, int(timeout_ms)), ctx.tool_limits.max_bash_timeout_ms)
        timeout_seconds = max(0.001, timeout_ms / 1000)
        wrapped = command + _state_trailer()

        result = run_process(
            ["bash", "-c", wrapped],
            cwd,
            timeout_seconds,
            merge_stderr=True,
            max_output_chars=ctx.tool_limits.max_process_output_chars,
        )
        self._invalidate_reads(ctx, command, cwd)

        visible, cwd_text = _extract_cwd(result.stdout)
        cwd_note = self._update_cwd(ctx, cwd_text)

        output = _truncate_head_tail(visible.strip(), ctx.tool_limits.max_tool_output_chars) or "(无输出)"
        display_cwd = display(ctx.workspace, ctx.workspace_access.bash_cwd or ctx.workspace)

        lines = [f"退出码：{result.exit_code}", f"目录：{display_cwd}"]
        if result.timed_out:
            lines.append(f"执行超时（超过 {timeout_ms}ms），进程已被终止。")
        if result.output_capped:
            lines.append("输出超过内存上限，进程已被终止，仅保留已捕获的开头。")
        if cwd_note:
            lines.append(cwd_note)
        lines.append(f"\n{output}")
        content = "\n".join(lines)

        metadata = {"exit_code": result.exit_code, "cwd": display_cwd}
        if result.timed_out:
            # 进程被杀掉，工具没有正常跑完；非零退出则是命令自己的结果，仍算工具成功。
            return ToolRunResult.failure(content, **metadata)
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
            ctx.workspace_access.bash_cwd = new_cwd
            return None
        ctx.workspace_access.bash_cwd = None
        return (
            f"注意：命令结束后所在目录越出了工作区，已重置回工作目录根 "
            f"{display(ctx.workspace, ctx.workspace)}"
        )

    def _invalidate_reads(self, ctx: ToolContext, command: str, cwd: Path) -> None:
        """bash 写过的文件不再算「已读」；看不清目标时清空全部已读标记。"""
        analysis = analyze_command(command)
        if analysis.opaque:
            ctx.workspace_access.clear_reads()
            return
        for path in written_paths(ctx.workspace, command, cwd):
            ctx.workspace_access.unmark_read(path)


def _truncate_head_tail(text: str, limit: int) -> str:
    """超限时保留头尾，中间打标记。pytest / 编译器把关键错误打在尾部。"""
    if len(text) <= limit:
        return text
    marker = "\n...[truncated]...\n"
    keep = limit - len(marker)
    if keep < 2:
        return text[:limit] + "...[truncated]"
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + text[-tail:]


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
