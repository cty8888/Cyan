"""SlashCommandCompleter：输入 "/" 后随打字实时过滤候选，不用按 Tab。"""

from __future__ import annotations

from prompt_toolkit.document import Document

from cyan.cli.commands import CommandRegistry, SlashCommand
from cyan.cli.completion import (
    FileReferenceCompleter,
    InputCompleter,
    SlashCommandCompleter,
    at_reference_prefix,
)


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


# ------------------------------------------------------------ at_reference_prefix


def test_at_reference_prefix_extracts_text_after_at():
    assert at_reference_prefix("看看 @src/a") == "src/a"


def test_at_reference_prefix_none_without_at():
    assert at_reference_prefix("普通任务") is None


def test_at_reference_prefix_none_when_at_is_mid_word():
    """"foo@bar" 这种邮箱一样的写法，"@" 前一个字符不是空白，不算文件引用。"""
    assert at_reference_prefix("联系 foo@bar") is None


def test_at_reference_prefix_allows_start_of_line():
    assert at_reference_prefix("@a") == "a"


def test_at_reference_prefix_none_after_space_in_token():
    assert at_reference_prefix("@a.py 之后还有话") is None


def test_at_reference_prefix_empty_right_after_at():
    assert at_reference_prefix("看看 @") == ""


# ------------------------------------------------------------ FileReferenceCompleter


def _complete_files(workspace, text: str) -> list[str]:
    completer = FileReferenceCompleter(workspace)
    document = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(document, complete_event=None)]


def test_file_reference_lists_matching_files(tmp_path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.py").write_text("", encoding="utf-8")

    names = _complete_files(tmp_path, "看看 @a")

    assert names == ["a.py"]


def test_file_reference_finds_nested_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")

    names = _complete_files(tmp_path, "@src/main")

    assert names == ["src/main.py"]


def test_file_reference_skips_ignored_dirs(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("", encoding="utf-8")

    names = _complete_files(tmp_path, "@")

    assert names == []


def test_file_reference_no_completions_without_at():
    completer = FileReferenceCompleter("/tmp")
    document = Document(text="普通任务", cursor_position=4)
    assert list(completer.get_completions(document, complete_event=None)) == []


# ------------------------------------------------------------ InputCompleter


def test_input_completer_dispatches_slash_when_line_starts_with_slash(tmp_path):
    completer = InputCompleter(_registry(), tmp_path)
    text = "/to"
    document = Document(text=text, cursor_position=len(text))
    names = [c.text for c in completer.get_completions(document, complete_event=None)]
    assert names == ["/todos"]


def test_input_completer_dispatches_file_when_at_prefix(tmp_path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    completer = InputCompleter(_registry(), tmp_path)
    text = "看看 @a"
    document = Document(text=text, cursor_position=len(text))
    names = [c.text for c in completer.get_completions(document, complete_event=None)]
    assert names == ["a.py"]


def test_input_completer_yields_nothing_for_plain_text(tmp_path):
    completer = InputCompleter(_registry(), tmp_path)
    text = "普通任务"
    document = Document(text=text, cursor_position=len(text))
    assert list(completer.get_completions(document, complete_event=None)) == []
