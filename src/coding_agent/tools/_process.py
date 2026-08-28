"""子进程执行辅助：bash 工具与测试共用。"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ProcessOutput:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration: float


def run_process(
    argv: list[str] | str,
    cwd: Path,
    timeout: float,
    shell: bool = False,
    env: dict[str, str] | None = None,
    merge_stderr: bool = False,
) -> ProcessOutput:
    """启动子进程并在超时时连同其整个进程组一起结束。"""
    started = time.monotonic()
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT if merge_stderr else subprocess.PIPE,
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
        terminate(process)
        stdout, stderr = process.communicate()
    except BaseException:
        # 子进程在独立进程组里，收不到终端的 SIGINT。用户按 Ctrl-C 时若不显式清理，
        # 它会脱离本进程继续运行成为孤儿。
        terminate(process)
        raise

    return ProcessOutput(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout or "",
        stderr="" if merge_stderr else (stderr or ""),
        timed_out=timed_out,
        duration=time.monotonic() - started,
    )


def terminate(process: subprocess.Popen) -> None:
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
