"""Python 执行器：使用当前解释器，保证与项目虚拟环境一致。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .base import CodeRunner


class PythonRunner(CodeRunner):
    language = "python"
    display_name = "Python"
    extension = ".py"

    def build_command(self, script: Path) -> list[str]:
        # -u 关闭缓冲，确保被截断/超时时也能拿到已产生的输出
        return [sys.executable, "-u", str(script)]

    def environment(self, workspace: Path) -> dict[str, str]:
        # 临时片段存放在 .coding_agent/tmp 下，sys.path[0] 会指向那里而非工作目录，
        # 因此显式把工作目录加进 PYTHONPATH，片段才能 import 项目自身的模块。
        existing = os.environ.get("PYTHONPATH", "")
        path = str(workspace)
        return {"PYTHONPATH": f"{path}{os.pathsep}{existing}" if existing else path}

    def executable(self) -> str | None:
        return sys.executable
