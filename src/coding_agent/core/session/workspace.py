"""Agent 工作环境状态，不执行文件操作。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionWorkspace:
    # TODO: 与 Config.workspace 约定单一权威来源，避免 root 双写不一致
    root: Path = field(default_factory=Path.cwd)
    cwd: Path | None = None
    opened_files: set[Path] = field(default_factory=set, repr=False)
    # TODO: write_file / edit_file 完成后调用 mark_modified()
    modified_files: set[Path] = field(default_factory=set, repr=False)
    environment: dict[str, Any] = field(default_factory=dict)
