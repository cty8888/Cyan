"""子进程执行 —— bash 工具与测试共用。"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .types import ProcessOutput


def run_process(
    argv: list[str] | str,
    cwd: Path,
    timeout: float,
    shell: bool = False,
    env: dict[str, str] | None = None,
    merge_stderr: bool = False,
) -> ProcessOutput:
    """启动子进程；超时时连同进程组一起终止。"""
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
        # 独立进程组，超时时可连子孙进程一起清理
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
        # 子进程在独立进程组里，收不到终端 SIGINT；显式清理避免孤儿进程
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
    """终止进程及其进程组，必要时强制 kill。"""
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
