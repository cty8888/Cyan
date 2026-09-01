"""App._build_prompt_session：真实跑一遍 prompt_toolkit 的补全管线（不只是单测

``SlashCommandCompleter`` 本身），确认两个坑修好了：

1. 删除字符（比如 "/he" 退格成 "/h"）要能重新算出新的候选，不能卡死在删除前
   那一份候选上——内置的 ``complete_while_typing`` 只在插入字符时才会重算。
2. 没有候选时不该再预留一块空白菜单区域（看起来像个挥之不去的灰框）——这个
   预留空间只应该在真的有 ``complete_state`` 时才出现。
"""

from __future__ import annotations

import asyncio
import contextlib

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cyan.cli.app import App
from cyan.cli.commands import build_default_commands


class _FakeSettings:
    def __init__(self, workspace) -> None:
        self.workspace = workspace


class _FakeCommandsApp:
    """够 ``App._build_prompt_session`` 用 duck typing 调用的最小 App：装 commands 与 settings.workspace。"""

    def __init__(self, commands, workspace) -> None:
        self.commands = commands
        self.settings = _FakeSettings(workspace)


def _completion_names(session) -> list[str]:
    state = session.default_buffer.complete_state
    return [c.text for c in state.completions] if state else []


def test_backspace_recomputes_completions(tmp_path):
    """退格删字符后，候选要跟着更新，不能停留在删除前的那一份上。"""

    async def scenario() -> tuple[list[str], list[str], str]:
        fake_app = _FakeCommandsApp(build_default_commands(), tmp_path)

        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                # PromptSession 在构造时就绑定当前 app session 的 input/output，
                # 必须在进了 create_app_session 上下文之后才能建，不然会绑到真实
                # 终端的 stdin 上，喂进去的按键根本到不了这个 session。
                session = App._build_prompt_session(fake_app, tmp_path)
                task = asyncio.ensure_future(session.app.run_async())
                await asyncio.sleep(0.05)

                pipe_input.send_text("/he")
                await asyncio.sleep(0.3)
                after_he = _completion_names(session)

                pipe_input.send_text("\x7f")  # 退格，删掉最后一个 "e"
                await asyncio.sleep(0.3)
                after_backspace = _completion_names(session)
                text_after_backspace = session.default_buffer.text

                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        return after_he, after_backspace, text_after_backspace

    after_he, after_backspace, text_after_backspace = asyncio.run(scenario())

    assert after_he == ["/help"]
    assert text_after_backspace == "/h"
    # "/h" 前缀能匹配到不止一个命令（/help、/history……），退格后应该重新看到
    # 完整的一份，而不是仍然停留在退格前只剩 "/help" 的那份。
    assert set(after_backspace) >= {"/help", "/history"}


def test_no_completions_reserved_space_clears_when_prefix_stops_matching(tmp_path):
    """删到不再以 "/" 开头（或者压根没匹配的候选）时，不该再挂着一份候选状态。"""

    async def scenario() -> object:
        fake_app = _FakeCommandsApp(build_default_commands(), tmp_path)

        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                session = App._build_prompt_session(fake_app, tmp_path)
                task = asyncio.ensure_future(session.app.run_async())
                await asyncio.sleep(0.05)

                pipe_input.send_text("/help")
                await asyncio.sleep(0.3)
                pipe_input.send_text("\x7f\x7f\x7f\x7f\x7f")  # 退格删空
                await asyncio.sleep(0.3)
                state_after_clear = session.default_buffer.complete_state

                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        return state_after_clear

    assert asyncio.run(scenario()) is None
