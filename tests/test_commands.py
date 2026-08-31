"""斜杠命令：/stream 直接改 LLMSettings.stream，下一次模型调用立刻生效。"""

from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console

from cyan.cli.commands import _cmd_stream
from cyan.cli.renderer import Renderer
from cyan.settings.llm import LLMSettings


def _fake_app(stream: bool = True) -> SimpleNamespace:
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    return SimpleNamespace(
        settings=SimpleNamespace(llm=LLMSettings(api_key="k", stream=stream)),
        renderer=Renderer(console),
    )


def test_stream_no_args_only_reports_state():
    app = _fake_app(stream=True)
    assert _cmd_stream(app, []) is False
    assert app.settings.llm.stream is True


def test_stream_off_disables_setting():
    app = _fake_app(stream=True)
    _cmd_stream(app, ["off"])
    assert app.settings.llm.stream is False


def test_stream_on_enables_setting():
    app = _fake_app(stream=False)
    _cmd_stream(app, ["on"])
    assert app.settings.llm.stream is True


def test_stream_invalid_argument_does_not_change_setting():
    app = _fake_app(stream=True)
    _cmd_stream(app, ["maybe"])
    assert app.settings.llm.stream is True
