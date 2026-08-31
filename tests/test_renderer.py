"""Renderer 的流式打字机渲染：节流、收尾定稿、中断清理。"""

from __future__ import annotations

import io

from rich.console import Console

from cyan.cli.renderer import Renderer, extract_partial_string_field


def _renderer() -> Renderer:
    return Renderer(Console(file=io.StringIO(), force_terminal=True, width=80))


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
