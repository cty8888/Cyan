"""SlashCommandCompleter：输入 "/" 后随打字实时过滤候选，不用按 Tab。"""

from __future__ import annotations

from prompt_toolkit.document import Document

from cyan.cli.commands import CommandRegistry, SlashCommand
from cyan.cli.completion import SlashCommandCompleter


def _noop_handler(app, args):
    return False


def _registry() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register(SlashCommand("/help", "/help", "显示本帮助", _noop_handler))
    registry.register(SlashCommand("/todos", "/todos [clear]", "查看当前任务清单", _noop_handler))
    registry.register(
        SlashCommand(
            "/resume",
            "/resume [<id 或前缀>]",
            "切换到另一个会话",
            _noop_handler,
            aliases=("/continue",),
        )
    )
    return registry


def _complete(text: str) -> list:
    completer = SlashCommandCompleter(_registry())
    document = Document(text=text, cursor_position=len(text))
    return list(completer.get_completions(document, complete_event=None))


def test_bare_slash_lists_all_commands():
    completions = _complete("/")
    names = {c.text for c in completions}
    assert names == {"/help", "/todos", "/resume", "/continue"}


def test_prefix_filters_by_command_name():
    completions = _complete("/to")
    assert [c.text for c in completions] == ["/todos"]


def test_prefix_matches_alias_too():
    """``/continue`` 是 ``/resume`` 的别名，按别名前缀也要能补全到。"""
    completions = _complete("/con")
    assert [c.text for c in completions] == ["/continue"]


def test_completion_replaces_the_whole_typed_prefix():
    completions = _complete("/to")
    assert completions[0].start_position == -len("/to")


def test_completion_carries_usage_and_description_as_display_meta():
    completions = _complete("/todos")
    completion = completions[0]
    assert completion.display_meta_text == "查看当前任务清单"
    assert completion.display_text == "/todos [clear]"


def test_no_match_returns_no_completions():
    assert _complete("/zzz") == []


def test_not_starting_with_slash_yields_nothing():
    """自然语言任务输入不应该被误伤，不产出任何候选。"""
    assert _complete("帮我看看") == []


def test_space_after_command_name_stops_completing():
    """已经进入参数阶段（出现空格）时不再弹候选，不干扰参数输入。"""
    assert _complete("/todos ") == []
