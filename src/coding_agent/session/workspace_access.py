"""工具可用的工作区读写入口。

只向工具暴露执行时真正需要的能力：文件是否已读、标记已读/已修改、bash 工作目录。
``Session`` 的其余状态（消息历史、权限白名单、token 用量等）不对外透出。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session


@dataclass
class WorkspaceAccess:
    """包住一个 ``Session``，只转发工具真正需要的几个读写方法。"""

    _session: Session

    @property
    def bash_cwd(self) -> Path | None:
        return self._session.bash_cwd

    @bash_cwd.setter
    def bash_cwd(self, value: Path | None) -> None:
        self._session.bash_cwd = value

    def has_read(self, path: Path) -> bool:
        return self._session.has_read(path)

    def mark_read(self, path: Path) -> None:
        self._session.mark_read(path)

    def unmark_read(self, path: Path) -> None:
        self._session.unmark_read(path)

    def clear_reads(self) -> None:
        self._session.clear_reads()

    def mark_modified(self, path: Path) -> None:
        self._session.mark_modified(path)
