"""执行类工具：shell 命令与源码运行。

所有子进程都满足三条约束：工作目录锁定在沙箱内、有超时上限、输出会被截断。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import InvalidToolArgumentsError, ToolError
from ..security.policy import SecurityPolicy
from .base import RiskLevel, Tool, ToolContext, ToolResult
from .runners import get_runner, supported_languages


@dataclass
class ProcessOutput:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration: float


def _run_process(
    argv: list[str] | str,
    cwd: Path,
    timeout: int,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> ProcessOutput:
    """启动子进程并在超时时连同其整个进程组一起结束。"""
    started = time.monotonic()
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": shell,
        "env": {**os.environ, **env} if env else None,
    }
    if os.name == "posix":
        # 独立进程组，超时后能连子孙进程一起清理，避免 shell 管道留下孤儿进程
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(argv, **popen_kwargs)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        stdout, stderr = process.communicate()
    except BaseException:
        # 子进程在独立进程组里，收不到终端的 SIGINT。用户按 Ctrl-C 时若不显式清理，
        # 它会脱离本进程继续运行成为孤儿。
        _terminate(process)
        raise

    return ProcessOutput(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        duration=time.monotonic() - started,
    )


def _terminate(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... (省略 {omitted} 个字符) ...\n{tail}"


def _format_output(result: ProcessOutput, limit: int, timeout: int) -> str:
    lines = [f"退出码：{result.exit_code}（耗时 {result.duration:.1f}s）"]
    if result.timed_out:
        lines.append(f"执行超时（超过 {timeout} 秒），进程已被终止。")

    stdout = _truncate(result.stdout.strip(), limit)
    stderr = _truncate(result.stderr.strip(), limit)
    if stdout:
        lines.append(f"\n[stdout]\n{stdout}")
    if stderr:
        lines.append(f"\n[stderr]\n{stderr}")
    if not stdout and not stderr:
        lines.append("(无输出)")
    return "\n".join(lines)


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在工作目录下执行 shell 命令，返回退出码与标准输出/错误输出。"
        "适合跑测试框架、git、构建、包管理等 shell 操作。"
        "若只是想运行一段项目代码或验证片段，改用 run_code，它会自动选对解释器并配好 import 路径。"
        "命令有超时限制，无法访问工作目录之外的位置，且禁止执行需要交互式输入的命令。"
    )
    risk = RiskLevel.EXEC
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "cwd": {
                "type": "string",
                "description": "执行目录，相对于工作目录。默认为工作目录根。",
                "default": ".",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 60，最大 600。",
            },
        },
        "required": ["command"],
    }

    def describe(self, args: dict[str, Any], policy: SecurityPolicy) -> tuple[str, str | None, str]:
        command = str(args.get("command", ""))
        cwd = str(args.get("cwd", "."))
        location = "" if cwd in {".", ""} else f"（在 {cwd} 下）"
        return f"执行命令{location}", command, "shell"

    def run(self, ctx: ToolContext, command: str, cwd: str = ".", timeout: int | None = None) -> ToolResult:
        ctx.policy.check_command(command)

        work_dir = ctx.policy.resolve_path(cwd, must_exist=True)
        if not work_dir.is_dir():
            raise ToolError(f"{ctx.policy.display(work_dir)} 不是目录")

        limit_seconds = min(int(timeout or ctx.config.command_timeout), 600)
        result = _run_process(command, work_dir, limit_seconds, shell=True)

        content = _format_output(result, ctx.config.max_tool_output_chars, limit_seconds)
        if result.timed_out or result.exit_code != 0:
            return ToolResult(ok=False, error=content, metadata={"exit_code": result.exit_code})
        return ToolResult.success(content, exit_code=result.exit_code)


class RunCodeTool(Tool):
    name = "run_code"
    description = (
        f"执行源代码文件或代码片段，当前支持的语言：{', '.join(supported_languages())}。"
        "二选一：传 path 执行工作目录下已存在的文件，或传 code 执行临时片段。"
        "相比 run_command，它会自动使用本机可用的解释器，并把工作目录加入 import 搜索路径，"
        "所以片段里可以直接 import 项目自身的模块。验证实现是否正确时优先用它。"
    )
    risk = RiskLevel.EXEC
    parameters = {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "代码语言",
                "enum": supported_languages(),
                "default": "python",
            },
            "path": {"type": "string", "description": "要执行的源码文件路径（与 code 二选一）"},
            "code": {"type": "string", "description": "要执行的代码片段（与 path 二选一）"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600。"},
        },
    }

    def describe(self, args: dict[str, Any], policy: SecurityPolicy) -> tuple[str, str | None, str]:
        language = str(args.get("language", "python"))
        if args.get("path"):
            return f"运行 {language} 文件 {args['path']}", None, "text"
        return f"运行 {language} 代码片段", str(args.get("code", "")), language

    def run(
        self,
        ctx: ToolContext,
        language: str = "python",
        path: str | None = None,
        code: str | None = None,
        timeout: int | None = None,
    ) -> ToolResult:
        if bool(path) == bool(code):
            raise InvalidToolArgumentsError("path 与 code 必须且只能提供其中一个")

        runner = get_runner(language)
        if not runner.is_available():
            raise ToolError(f"环境中找不到 {runner.display_name} 解释器，无法执行 {language} 代码")

        limit_seconds = min(int(timeout or ctx.config.command_timeout), 600)
        temp_script: Path | None = None

        if path:
            script = ctx.policy.resolve_path(path, must_exist=True)
            if not script.is_file():
                raise ToolError(f"{ctx.policy.display(script)} 不是文件")
        else:
            temp_dir = ctx.config.state_dir / "tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            script = temp_dir / f"snippet_{uuid.uuid4().hex[:8]}{runner.extension}"
            script.write_text(code or "", encoding="utf-8")
            temp_script = script

        try:
            result = _run_process(
                runner.build_command(script),
                ctx.workspace,
                limit_seconds,
                env=runner.environment(ctx.workspace),
            )
        finally:
            if temp_script is not None:
                temp_script.unlink(missing_ok=True)

        content = _format_output(result, ctx.config.max_tool_output_chars, limit_seconds)
        if result.timed_out or result.exit_code != 0:
            return ToolResult(ok=False, error=content, metadata={"exit_code": result.exit_code})
        return ToolResult.success(content, exit_code=result.exit_code)
