"""工具可用的任务清单入口。

跟 ``WorkspaceAccess`` 一样只包一层：``todo_write`` 只需要「读当前清单、整体替换」
两个操作，不需要 ``Session`` 的其它状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types import TodoItem

if TYPE_CHECKING:
    from .session import Session


@dataclass
class TodoAccess:
    """包住一个 ``Session``，只转发任务清单的读写方法。"""

    _session: Session

    @property
    def items(self) -> list[TodoItem]:
        return self._session.todos

    def set(self, items: list[TodoItem]) -> None:
        self._session.set_todos(items)
