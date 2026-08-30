"""自动记忆的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MemoryKind(Enum):
    """四类项目级笔记，对应 Claude Code 的 user / feedback / project / reference。"""

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


INDEX_FILENAME = "MEMORY.md"
KIND_FILENAMES: dict[MemoryKind, str] = {
    MemoryKind.USER: "user.md",
    MemoryKind.FEEDBACK: "feedback.md",
    MemoryKind.PROJECT: "project.md",
    MemoryKind.REFERENCE: "reference.md",
}
ALLOWED_FILENAMES = frozenset({INDEX_FILENAME, *KIND_FILENAMES.values()})

MAX_INDEX_LINES = 200
MAX_INDEX_CHARS = 25_000

ENV_DISABLE = "CYAN_DISABLE_AUTO_MEMORY"


@dataclass
class MemoryEntry:
    """一条待写入的记忆。``summary`` 进索引；``detail`` 进类型文件。"""

    kind: MemoryKind
    summary: str
    detail: str = ""
