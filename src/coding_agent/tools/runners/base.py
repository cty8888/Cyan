"""代码执行器抽象。

``run_code`` 工具本身不关心语言，只负责查表分发；
新增一门语言 = 实现一个 ``CodeRunner`` 子类并注册，工具定义与 Agent Loop 都不用改。
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class CodeRunner(ABC):
    language: str = ""
    display_name: str = ""
    extension: str = ""

    @abstractmethod
    def build_command(self, script: Path) -> list[str]:
        """给定脚本路径，返回用于执行它的 argv。"""

    def environment(self, workspace: Path) -> dict[str, str]:
        """需要追加到子进程的环境变量。默认不追加。"""
        return {}

    def executable(self) -> str | None:
        """返回该语言解释器/编译器的可执行文件路径，找不到则返回 None。"""
        argv = self.build_command(Path("_probe"))
        if not argv:
            return None
        first = argv[0]
        return first if Path(first).is_file() else shutil.which(first)

    def is_available(self) -> bool:
        return self.executable() is not None
