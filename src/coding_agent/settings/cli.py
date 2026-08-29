"""命令行界面与进程行为。"""

from __future__ import annotations

from dataclasses import dataclass

from ..security.types import PermissionMode


@dataclass
class CliSettings:
    log_level: str = "INFO"
    verbose: bool = False
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    state_dirname: str = ".coding_agent"
