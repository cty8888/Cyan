"""bash 工具：唯一的 shell 执行入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..security.policy import SecurityPolicy
from ._process import run_process
from .base import RiskLevel, Tool, ToolContext, ToolResult

# 每条命令结束后，用这一行把最终所在目录带出来，供下一次调用延续 cwd。
# 用可读字符串而不是控制字符，方便调试；真实命令输出里几乎不可能撞到这个串。
_CWD_MARKER = "@@CODING_AGENT_CWD@@"


class BashTool(Tool):
    name = "bash"
    description = (
        "在项目工作目录下执行 shell 命令（测试、构建、git、脚本等）。"
        "每条命令都在独立的新进程里运行；工作目录会在调用之间延续——"
        "命令里执行了 cd 并且最终停在工作目录内，下一次调用会从那个目录继续，"
        "越出工作目录会被重置回工作目录根。不会保留环境变量或 shell 别名，"
        "命令里的 export 不会影响下一次调用。"
    )
    risk = RiskLevel.EXEC
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout_ms": {
                "type": "integer",
                "description": "超时毫秒数，默认 120000（120 秒）。",
                "default": 120_000,
            },
        },
        "required": ["command"],
    }

    def describe(self, args: dict[str, Any], policy: SecurityPolicy) -> tuple[str, str | None, str]:
        return "执行命令", str(args.get("command", "")), "shell"

    def run(self, ctx: ToolContext, command: str, timeout_ms: int = 120_000) -> ToolResult:
        ctx.policy.check_command(command)

        cwd = ctx.session.bash_cwd or ctx.workspace
        if not cwd.is_dir():
            # 上一次记录的目录被删掉了之类的边缘情况，退回工作目录根
            cwd = ctx.workspace
            ctx.session.bash_cwd = None

        timeout_seconds = max(0.001, int(timeout_ms) / 1000)
        wrapped = command + _state_trailer()

        result = run_process(["bash", "-c", wrapped], cwd, timeout_seconds, merge_stderr=True)

        visible, cwd_text = _extract_cwd(result.stdout)
        cwd_note = self._update_cwd(ctx, cwd_text)

        output = _truncate_tail(visible.strip(), ctx.config.max_tool_output_chars) or "(无输出)"
        display_cwd = ctx.policy.display(ctx.session.bash_cwd or ctx.workspace)

        lines = [f"退出码：{result.exit_code}", f"目录：{display_cwd}"]
        if result.timed_out:
            lines.append(f"执行超时（超过 {timeout_ms}ms），进程已被终止。")
        if cwd_note:
            lines.append(cwd_note)
        lines.append(f"\n{output}")
        content = "\n".join(lines)

        metadata = {"exit_code": result.exit_code, "cwd": display_cwd}
        if result.timed_out or result.exit_code != 0:
            return ToolResult(ok=False, error=content, metadata=metadata)
        return ToolResult.success(content, **metadata)

    def _update_cwd(self, ctx: ToolContext, cwd_text: str | None) -> str | None:
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
        return f"注意：命令结束后所在目录越出了工作区，已重置回工作目录根 {ctx.policy.display(ctx.workspace)}"


def _truncate_tail(text: str, limit: int) -> str:
    """只保留开头 limit 个字符，超出部分直接砍掉——比头尾各留一半更符合日志类输出的阅读习惯。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _state_trailer() -> str:
    """追加在用户命令之后的一段脚本：保留原始退出码，同时把命令结束后的 $PWD 打印出来。"""
    return f'\n__ca_exit=$?\nprintf "\\n{_CWD_MARKER}%s\\n" "$PWD"\nexit "$__ca_exit"\n'


def _extract_cwd(output: str) -> tuple[str, str | None]:
    """从合并输出里摘掉末尾的 cwd 标记行，返回 (展示给模型的内容, 命令结束后的目录)。"""
    idx = output.rfind(_CWD_MARKER)
    if idx == -1:
        return output, None
    visible = output[:idx]
    remainder = output[idx + len(_CWD_MARKER) :]
    cwd_line = remainder.splitlines()[0] if remainder.splitlines() else ""
    return visible, cwd_line.strip() or None
