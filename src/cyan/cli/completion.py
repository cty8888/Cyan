"""斜杠命令与 ``@`` 文件引用的实时补全。

跟 ``PromptSession(complete_while_typing=True)`` 搭配使用：输入 "/" 或 "@" 后
不用按 Tab，随打字自动弹出、实时过滤的候选列表就是这里产出的。具体接线（怎么
监听删除字符重算候选）见 ``cli/app.py._build_prompt_session``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

if TYPE_CHECKING:
    from .commands import CommandRegistry

# 补全时跳过的目录：版本控制、依赖、缓存产物，翻这些目录既慢又没意义。
_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}
_MAX_FILE_CANDIDATES = 30


class SlashCommandCompleter(Completer):
    """只在还在敲命令名（"/xxx"，不含空格）时给候选。

    一旦出现空格（已经进入参数阶段）或者输入不是以 "/" 开头（自然语言任务），
    直接不产出任何候选，不打断正常输入。
    """

    def __init__(self, commands: "CommandRegistry") -> None:
        self._commands = commands

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for command in self._commands:
            for name in (command.name, *command.aliases):
                if name.startswith(text):
                    yield Completion(
                        name,
                        start_position=-len(text),
                        display=command.usage,
                        display_meta=command.description,
                    )


def at_reference_prefix(text: str) -> str | None:
    """若光标前正处在一个以 "@" 起头的词里，返回 "@" 之后已输入的部分；否则 None。

    只在词的起点触发（前一个字符是空白或就是行首），避免邮箱地址一类正常文本
    里出现的 "@" 被误当成文件引用而弹出候选。
    """
    at_index = text.rfind("@")
    if at_index == -1:
        return None
    if at_index > 0 and not text[at_index - 1].isspace():
        return None
    prefix = text[at_index + 1 :]
    if " " in prefix:
        return None
    return prefix


class FileReferenceCompleter(Completer):
    """给 "@path" 候选：从工作区里找相对路径以已输入前缀开头的文件。

    候选按目录深度优先遍历、按名字排序，命中数量封顶 ``_MAX_FILE_CANDIDATES``——
    只是键盘导航的候选列表，不追求穷举，扫到够用的一批就停。
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        prefix = at_reference_prefix(document.text_before_cursor)
        if prefix is None:
            return
        for relative in _iter_file_candidates(self._workspace, prefix):
            yield Completion(relative, start_position=-len(prefix), display=relative)


def _iter_file_candidates(workspace: Path, prefix: str) -> Iterable[str]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            relative = str((Path(dirpath) / name).relative_to(workspace))
            if not relative.startswith(prefix):
                continue
            yield relative
            count += 1
            if count >= _MAX_FILE_CANDIDATES:
                return


class InputCompleter(Completer):
    """按输入内容分派：整行是斜杠命令前缀走命令补全，否则按 "@" 词走文件补全。

    二者触发条件互斥（一个要求整行以 "/" 开头，一个要求光标词以 "@" 开头），
    同一次按键最多命中一种，不会互相干扰。
    """

    def __init__(self, commands: "CommandRegistry", workspace: Path) -> None:
        self._slash = SlashCommandCompleter(commands)
        self._file = FileReferenceCompleter(workspace)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            yield from self._slash.get_completions(document, complete_event)
            return
        yield from self._file.get_completions(document, complete_event)
