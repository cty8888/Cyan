"""Agent 工作环境状态，不执行文件操作。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionWorkspace:
    root: Path = field(default_factory=Path.cwd)
    cwd: Path | None = None
    opened_files: set[Path] = field(default_factory=set, repr=False)
    modified_files: set[Path] = field(default_factory=set, repr=False)
    environment: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_root(cls, root: Path) -> SessionWorkspace:
        """绑定项目根目录，其余字段使用默认值。"""
        return cls(root=root.resolve())
