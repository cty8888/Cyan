"""App._render：确保任意非流式分片事件渲染前都会收尾挂着的 Live。

流式过程中如果中途报错（比如 LLMError），事件流会跳过 AssistantReply/ToolStarted
直接落到 Notice——如果 Notice 渲染前不兜底收尾，没打完的 Live 就会一直挂着，
跟 Notice 的输出挤在一起，终端会花掉。
"""

from __future__ import annotations

import io

from rich.console import Console

from cyan.cli.app import App
from cyan.cli.renderer import Renderer
from cyan.core.types import AssistantReplyDelta, Notice, ToolCallDelta


class _FakeApp:
    """只装一个 renderer 属性，够 App._render 用 duck typing 调用了。"""

    def __init__(self, renderer: Renderer) -> None:
        self.renderer = renderer


def _renderer() -> Renderer:
    return Renderer(Console(file=io.StringIO(), force_terminal=True, width=80))


def test_notice_after_text_stream_error_stops_dangling_live():
    """流式文本中途报错时，随后的 Notice 要先收尾挂着的 Live。"""
    renderer = _renderer()
    app = _FakeApp(renderer)
    App._render(app, AssistantReplyDelta(text="部分文本"))
    assert renderer._live is not None

    App._render(app, Notice(message="模型调用失败：连接中断", level="error"))
    assert renderer._live is None
    assert renderer._live_buffer == ""


def test_notice_after_tool_call_delta_stream_error_stops_dangling_preview():
    """流式 tool_call 参数中途报错时，随后的 Notice 要先收尾挂着的工具预览 Live。"""
    renderer = _renderer()
    app = _FakeApp(renderer)
    App._render(app, ToolCallDelta(index=0, call_id="call_1", name="write_file", arguments_delta='{"path": "a.py"'))
    assert renderer._live is not None

    App._render(app, Notice(message="模型调用失败：连接中断", level="error"))
    assert renderer._live is None
    assert renderer._tool_preview is None


def test_consecutive_deltas_do_not_get_stopped_by_the_guard():
    """连续的流式分片事件之间不应该被这个兜底逻辑误伤，Live 得一直保持打开。"""
    renderer = _renderer()
    app = _FakeApp(renderer)
    App._render(app, AssistantReplyDelta(text="第一段"))
    live_after_first = renderer._live
    App._render(app, AssistantReplyDelta(text="第二段"))
    assert renderer._live is live_after_first
    assert renderer._live_buffer == "第一段第二段"
