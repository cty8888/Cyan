"""斜杠命令的实时补全。

跟 ``PromptSession(complete_while_typing=True)`` 搭配使用：输入 "/" 后不用按
Tab，随打字自动弹出、实时过滤的候选列表就是这里产出的。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

if TYPE_CHECKING:
    from .commands import CommandRegistry


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
