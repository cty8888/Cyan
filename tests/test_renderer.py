"""Renderer 的流式打字机渲染：节流、收尾定稿、中断清理。"""

from __future__ import annotations

import io
from pathlib import Path

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from cyan.cli.renderer import (
    Renderer,
    _format_args,
    _ToolPreviewState,
    _tool_preview_panel,
    extract_partial_string_field,
    render_todo_lines,
)
from cyan.security.types import PermissionMode
from cyan.settings import AgentSettings, LLMSettings
from cyan.tools.types import ToolRunResult


def _renderer() -> Renderer:
    return Renderer(Console(file=io.StringIO(), force_terminal=True, width=80))


def _plain(text: str) -> str:
    """去掉语法高亮插入的 ANSI，按可见字符断言。"""
    return Text.from_ansi(text).plain


def _render_banner(width: int, workspace: Path) -> str:
    """在给定终端宽度下渲染一次启动横幅，返回纯文本输出，供跨宽度对比。

    ``width`` 和 ``height`` 要一起传：rich 的 ``Console.size`` 只有两个都显式给了
    才会用这两个值，否则会去探测真实终端尺寸（在非 tty 的测试环境里探测不到，会
    悄悄退回默认的 80），传了 ``width`` 却没传 ``height`` 就会被这条规则坑到。
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=width, height=24, color_system=None)
    renderer = Renderer(console)
    settings = AgentSettings(
        workspace=workspace,
        llm=LLMSettings(model="deepseek-chat", api_key="x", base_url="http://x"),
    )
    renderer.banner(settings, PermissionMode.DEFAULT)
    return buf.getvalue()


def test_assistant_delta_starts_live_and_buffers():
    renderer = _renderer()
    renderer.assistant_delta("Hello")
    assert renderer._live is not None
    assert renderer._live_buffer == "Hello"
    renderer.stop_live_preview()


def test_assistant_delta_throttles_rerender(monkeypatch):
    """短时间内的连续分片只累积缓冲区，不必每次都重新解析 Markdown。"""
    renderer = _renderer()
    times = iter([100.0, 100.02, 100.03, 100.20])
    monkeypatch.setattr("cyan.cli.renderer.time.monotonic", lambda: next(times))

    renderer.assistant_delta("a")  # 首次：立刻渲染
    assert renderer._live_last_update == 100.0

    renderer.assistant_delta("b")  # 0.02s 后：跳过重绘
    assert renderer._live_last_update == 100.0

    renderer.assistant_delta("c")  # 0.03s 后：跳过重绘
    assert renderer._live_last_update == 100.0

    renderer.assistant_delta("d")  # 0.20s 后：超过节流间隔，重绘
    assert renderer._live_last_update == 100.20

    assert renderer._live_buffer == "abcd"
    renderer.stop_live_preview()


def test_stop_live_flushes_buffer_and_resets_state():
    renderer = _renderer()
    renderer.assistant_delta("partial")
    renderer.stop_live_preview()
    assert renderer._live is None
    assert renderer._live_buffer == ""
    assert renderer._live_last_update == 0.0


def test_abort_live_without_active_stream_is_noop():
    renderer = _renderer()
    renderer.abort_live()
    assert renderer._live is None


def test_abort_live_stops_dangling_live_session():
    renderer = _renderer()
    renderer.assistant_delta("正在生成中")
    assert renderer._live is not None
    renderer.abort_live()
    assert renderer._live is None
    assert renderer._live_buffer == ""


def test_assistant_finalizes_and_stops_any_active_live():
    renderer = _renderer()
    renderer.assistant_delta("partial")
    assert renderer._live is not None
    renderer.assistant("完整最终文本")
    assert renderer._live is None


def test_assistant_without_prior_delta_still_works():
    """没有流式增量（比如 stream=False 退化路径）时，assistant() 单独调用不受影响。"""
    renderer = _renderer()
    renderer.assistant("直接完整输出")
    assert renderer._live is None


def test_tool_call_delta_starts_live_and_buffers_arguments():
    renderer = _renderer()
    renderer.tool_call_delta(0, "call_1", "write_file", '{"path": "a.py", "content": "hi"')
    assert renderer._live is not None
    assert renderer._live_kind == "tool"
    assert renderer._tool_preview.name == "write_file"
    assert renderer._tool_preview.call_id == "call_1"
    assert renderer._tool_preview.arguments == '{"path": "a.py", "content": "hi"'
    renderer.stop_live_preview()


def test_tool_call_delta_switching_index_resets_preview_state():
    """模型换到另一个 tool_call（index 变化）时，旧的预览缓冲区要被丢弃重开。"""
    renderer = _renderer()
    renderer.tool_call_delta(0, "call_1", "write_file", '{"path": "a.py"')
    renderer.tool_call_delta(1, "call_2", "bash", '{"command": "ls"')
    assert renderer._tool_preview.index == 1
    assert renderer._tool_preview.name == "bash"
    assert renderer._tool_preview.arguments == '{"command": "ls"'
    renderer.stop_live_preview()


def test_tool_call_delta_switching_index_freezes_previous_preview():
    """换到下一个 tool_call 前，上一个工具的参数已经拼完，要定格成一条静态记录，不是无声消失。"""
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    renderer = Renderer(console)
    renderer.tool_call_delta(0, "call_1", "write_file", '{"path": "a.py", "content": "x = 1"}')
    renderer.tool_call_delta(1, "call_2", "bash", '{"command": "ls"}')

    output = console.file.getvalue()
    assert "write_file" in output
    assert "a.py" in output
    # 定格快照打印出来之后，Live 对象已经关闭重开，只留着 index=1（bash）的新状态。
    assert renderer._tool_preview.index == 1
    assert renderer._tool_preview.name == "bash"
    renderer.stop_live_preview()


def test_tool_call_delta_after_text_delta_switches_live_kind():
    """先流文本、又开始流工具参数时，要收尾文本 Live 再开一个新的预览 Live。"""
    renderer = _renderer()
    renderer.assistant_delta("先说点什么")
    assert renderer._live_kind == "text"
    renderer.tool_call_delta(0, "call_1", "write_file", '{"path": "a.py"')
    assert renderer._live_kind == "tool"
    assert renderer._live_buffer == ""
    renderer.stop_live_preview()


def test_stop_live_preview_resets_tool_preview_state():
    renderer = _renderer()
    renderer.tool_call_delta(0, "call_1", "write_file", '{"path": "a.py"')
    renderer.stop_live_preview()
    assert renderer._live is None
    assert renderer._live_kind is None
    assert renderer._tool_preview is None


def test_tool_started_finalizes_pending_tool_preview():
    renderer = _renderer()
    renderer.tool_call_delta(0, "call_1", "write_file", '{"path": "a.py", "content": "hi"}')
    assert renderer._live is not None
    renderer.tool_started("write_file", {"path": "a.py", "content": "hi"})
    assert renderer._live is None
    assert renderer._tool_preview is None


def test_abort_live_stops_dangling_tool_preview():
    renderer = _renderer()
    renderer.tool_call_delta(0, "call_1", "write_file", '{"path": "a.py"')
    renderer.abort_live()
    assert renderer._live is None
    assert renderer._tool_preview is None


def test_extract_partial_string_field_not_found_yet():
    assert extract_partial_string_field('{"path": "a.py"', "content") is None


def test_extract_partial_string_field_returns_partial_value_mid_stream():
    """字段值本身还在流式生成（没遇到收尾的引号），返回目前已经解码出来的部分。"""
    partial = '{"path": "a.py", "content": "line1\\nline2'
    assert extract_partial_string_field(partial, "content") == "line1\nline2"


def test_extract_partial_string_field_stops_before_incomplete_escape():
    """反斜杠是当前缓冲区最后一个字符（转义序列还没流完）时，先不展示这半个字符。"""
    partial = '{"content": "abc\\'
    assert extract_partial_string_field(partial, "content") == "abc"


def test_extract_partial_string_field_stops_before_incomplete_unicode_escape():
    partial = '{"content": "abc\\u00'
    assert extract_partial_string_field(partial, "content") == "abc"


def test_extract_partial_string_field_decodes_unicode_escape_once_complete():
    partial = '{"content": "abc\\u0041def"'
    assert extract_partial_string_field(partial, "content") == "abcAdef"


def test_extract_partial_string_field_returns_complete_value_when_string_closed():
    partial = '{"path": "a.py", "content": "done"}'
    assert extract_partial_string_field(partial, "content") == "done"


def test_format_args_summarizes_todo_write():
    args = {
        "todos": [
            {"content": "写测试", "status": "completed", "activeForm": "正在写测试"},
            {"content": "补文档", "status": "in_progress", "activeForm": "正在补文档"},
            {"content": "发布", "status": "pending", "activeForm": "正在发布"},
        ]
    }
    assert _format_args("todo_write", args) == "3 项，1 完成，1 进行中"


def test_render_todo_lines_marks_status_glyphs():
    items = [
        {"content": "已完成任务", "status": "completed", "active_form": ""},
        {"content": "进行中任务", "status": "in_progress", "active_form": "正在做进行中任务"},
        {"content": "待办任务", "status": "pending", "active_form": ""},
    ]
    lines = render_todo_lines(items)
    assert len(lines) == 3
    assert "✓" in lines[0] and "已完成任务" in lines[0]
    assert "●" in lines[1] and "正在做进行中任务" in lines[1]
    assert "○" in lines[2] and "待办任务" in lines[2]


def test_tool_finished_renders_todo_checklist():
    renderer = _renderer()
    result = ToolRunResult.success(
        "[ ] 写测试",
        todos=[{"content": "写测试", "status": "pending", "active_form": ""}],
    )
    renderer.tool_finished("todo_write", result, 0.1)
    output = renderer.console.file.getvalue()
    assert "任务清单已更新" in output
    assert "写测试" in output


def test_tool_finished_reports_cleared_checklist():
    renderer = _renderer()
    result = ToolRunResult.success("", todos=[])
    renderer.tool_finished("todo_write", result, 0.1)
    assert "任务清单已清空" in renderer.console.file.getvalue()


def test_banner_draws_full_height_divider_between_columns(tmp_path):
    """左右栏之间的竖线要贯穿内容高度，不能只出现在第一行。"""
    output = _render_banner(100, tmp_path)
    divider_rows = [line for line in output.splitlines() if line.count("│") >= 3]
    assert len(divider_rows) >= 4
    assert any("Welcome back!" in line and "│" in line[1:-1] for line in divider_rows)


def test_banner_width_spans_full_terminal_width(tmp_path):
    """窄于上限时跟终端同宽；宽于 120 时封顶，避免超宽屏把两栏拉得太散。"""
    narrow = _render_banner(70, tmp_path)
    wide = _render_banner(220, tmp_path)
    narrow_widths = {cell_len(line) for line in narrow.splitlines() if line.strip()}
    wide_widths = {cell_len(line) for line in wide.splitlines() if line.strip()}
    assert narrow_widths == {70}
    assert wide_widths == {120}


def test_banner_truncates_long_workspace_path_instead_of_growing(tmp_path):
    """工作目录路径很长时，靠省略号截断，而不是把横幅撑得比终端还宽。"""
    long_dir = tmp_path
    for part in ("a-very-long-directory-name",) * 10:
        long_dir = long_dir / part
    long_dir.mkdir(parents=True)

    output = _render_banner(100, long_dir)
    lines = [line for line in output.splitlines() if line.strip()]
    widths = {cell_len(line) for line in lines}
    assert len(widths) == 1  # 每一行（包括长路径那一行）打印宽度都跟横幅整体宽度一致
    assert widths == {100}
    assert "…" in output
    assert str(long_dir) not in output  # 完整路径太长，必须被截断，不能整段塞进去


def test_banner_keeps_short_labels_intact_even_when_value_column_is_squeezed(tmp_path):
    """标签列（model/mode/project/instructions/skills）不该因为另一列内容太长被一起截断。"""
    long_dir = tmp_path / ("x" * 200)
    long_dir.mkdir()

    output = _render_banner(80, long_dir)
    for label in ("model", "mode", "project"):
        assert label in output


def test_banner_uses_english_labels_and_short_mode_name(tmp_path):
    """标签用小写英文；mode 只显示短名字（不带 /mode /status 那种中文括注）。"""
    output = _render_banner(100, tmp_path)
    assert "model" in output
    assert "mode" in output
    assert "project" in output
    assert "Default" in output
    assert "默认" not in output


def test_banner_shows_home_relative_cwd(monkeypatch, tmp_path):
    """工作目录在用户主目录下时，用 ``~`` 简写前缀，而不是打印完整绝对路径。"""
    home = tmp_path / "home"
    home.mkdir()
    workspace = home / "project"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    output = _render_banner(100, workspace)

    assert "~/project" in output
    assert str(workspace) not in output


def test_banner_shows_skill_count_line(tmp_path):
    """启用中的 skill 数量单独一行显示，不逐条列名字。"""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100, height=24, color_system=None)
    renderer = Renderer(console)
    settings = AgentSettings(
        workspace=tmp_path,
        llm=LLMSettings(model="deepseek-chat", api_key="x", base_url="http://x"),
    )
    renderer.banner(settings, PermissionMode.DEFAULT, skill_count=4)
    output = buf.getvalue()
    assert "skills" in output
    assert "4 enabled" in output


def test_banner_omits_skills_line_when_count_is_zero(tmp_path):
    output = _render_banner(100, tmp_path)
    assert "enabled" not in output


def test_banner_shows_version_and_rounded_corners(tmp_path):
    from cyan import __version__

    output = _render_banner(100, tmp_path)
    assert f"Cyan Agent v{__version__}" in output
    assert "╭" in output and "╮" in output and "╰" in output and "╯" in output


def test_banner_shows_status_ready_and_tips_and_whats_new(tmp_path):
    """右栏有 Tips / What's new，左栏字段含 status。"""
    output = _render_banner(100, tmp_path)
    assert "status" in output
    assert "ready" in output
    assert "Tips" in output
    assert "/help" in output
    assert "Ctrl-C" in output
    assert "What's new" in output


def test_banner_greets_with_welcome_back(tmp_path):
    output = _render_banner(100, tmp_path)
    assert "Welcome back!" in output


def test_tool_preview_panel_distinguishes_not_yet_arrived_from_empty_content():
    """content 字段还没流到（None）和已经流到但值是空字符串（""）要展示不同的提示，不能混为一谈。"""
    pending = _ToolPreviewState(index=0, name="write_file", arguments='{"path": "a.py"')
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    console.print(_tool_preview_panel(pending))
    assert "生成参数中" in console.file.getvalue()

    empty = _ToolPreviewState(index=0, name="write_file", arguments='{"path": "a.py", "content": ""}')
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    console.print(_tool_preview_panel(empty))
    assert "内容为空" in console.file.getvalue()


def test_tool_finished_shows_code_preview_for_read_file():
    """read_file 成功后要把 preview 元数据渲成语法高亮片段，而不是只有一行摘要。"""
    renderer = _renderer()
    result = ToolRunResult.success(
        "mod.py（共 2 行，当前展示 1-2 行）\n1 | def add(a, b):\n2 |     return a - b",
        total_lines=2,
        path="mod.py",
        preview="def add(a, b):\n    return a - b",
        preview_start=1,
    )
    renderer.tool_finished("read_file", result, 0.1)
    output = _plain(renderer.console.file.getvalue())
    assert "def add" in output
    assert "return a - b" in output


def test_tool_finished_read_file_marks_truncated_preview_with_ellipsis():
    """预览只截了文件的一部分时，要留个「...」提示还有更多内容，不能看起来像文件就这么长。"""
    renderer = _renderer()
    result = ToolRunResult.success(
        "big.py（共 50 行，当前展示 1-50 行）\n...",
        total_lines=50,
        path="big.py",
        preview="\n".join(f"x = {i}" for i in range(20)),
        preview_start=1,
    )
    renderer.tool_finished("read_file", result, 0.1)
    assert "..." in renderer.console.file.getvalue()


def test_tool_finished_skips_preview_panel_when_metadata_absent():
    """旧格式的 result（没有 preview 元数据）不该因为多读了 metadata 而报错。"""
    renderer = _renderer()
    result = ToolRunResult.success("some.py 文件存在，但内容为空。")
    renderer.tool_finished("read_file", result, 0.1)
    assert "文件存在" in renderer.console.file.getvalue()


def test_task_finished_renders_completed_panel_with_elapsed_time():
    from cyan.core.types import StopReason

    renderer = _renderer()
    renderer.task_finished(StopReason.COMPLETED, {"llm_calls": 3, "tool_calls": 4, "total_tokens": 999}, elapsed=5.2)
    output = renderer.console.file.getvalue()
    assert "任务完成" in output
    assert "5.2s" in output
    assert "999" in output


def test_task_finished_formats_elapsed_over_a_minute_as_minutes_and_seconds():
    from cyan.core.types import StopReason

    renderer = _renderer()
    renderer.task_finished(StopReason.MAX_ITERATIONS, {"llm_calls": 1, "tool_calls": 1, "total_tokens": 1}, elapsed=125)
    assert "2分5秒" in renderer.console.file.getvalue()
