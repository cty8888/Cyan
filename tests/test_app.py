"""App._render：确保任意非流式分片事件渲染前都会收尾挂着的 Live。

流式过程中如果中途报错（比如 LLMError），事件流会跳过 AssistantReply/ToolStarted
直接落到 Notice——如果 Notice 渲染前不兜底收尾，没打完的 Live 就会一直挂着，
跟 Notice 的输出挤在一起，终端会花掉。
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from rich.console import Console

from cyan.cli.app import App
from cyan.cli.renderer import Renderer
from cyan.core.types import (
    AgentEvent,
    AssistantReplyDelta,
    Notice,
    StopReason,
    TaskFinished,
    TaskStarted,
    Thinking,
    ToolCallDelta,
)
from cyan.prompt.types import PromptLayer, PromptLayerKind
from cyan.settings import ToolLimits


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


class _SpyRenderer(Renderer):
    """记录 ``waiting_spinner()`` 被调用的次数，不真的起转圈动画。"""

    def __init__(self, console: Console) -> None:
        super().__init__(console)
        self.spinner_calls = 0

    def waiting_spinner(self):  # noqa: ANN201 - 测试用，返回值类型跟基类一致即可
        self.spinner_calls += 1
        return contextlib.nullcontext()


class _FakeRuntime:
    """``run(task)`` 直接把预先准备好的事件序列吐出来，不跑真正的 Agent Loop。"""

    tool_limits = ToolLimits()

    def __init__(self, events: Iterable[AgentEvent]) -> None:
        self._events = list(events)
        self.received_tasks: list[str] = []

    def run(self, task: str, *, file_refs=None):
        self.received_tasks.append(task)
        yield from self._events


class _FakeSettings:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace


class _FakeExecuteApp:
    """够 ``App._execute`` 用 duck typing 调用的最小 App：只装 renderer/runtime/settings。"""

    def __init__(self, renderer: Renderer, events: Iterable[AgentEvent]) -> None:
        self.renderer = renderer
        self.runtime = _FakeRuntime(events)
        self.settings = _FakeSettings(Path("."))
        self._pending_skill_reminder: str | None = None

    def _render(self, event: AgentEvent, *, started_at: float | None = None):
        return None

    def _abort(self, stream, *, started_at: float | None = None) -> StopReason:
        stream.close()
        return StopReason.USER_ABORT


def test_execute_wraps_only_the_send_right_after_thinking():
    """只有紧跟在 Thinking 后面的那次 send() 才应该被转圈动画包住。"""
    renderer = _SpyRenderer(Console(file=io.StringIO(), force_terminal=True, width=80))
    events = [
        TaskStarted(task="写个测试"),
        Thinking(iteration=1),
        AssistantReplyDelta(text="部分文本"),
        TaskFinished(reason=StopReason.COMPLETED),
    ]
    app = _FakeExecuteApp(renderer, events)

    reason = App._execute(app, "写个测试")

    assert reason == StopReason.COMPLETED
    assert renderer.spinner_calls == 1


def test_execute_wraps_each_thinking_gap_across_iterations():
    """多轮迭代时，每一次 Thinking 之后都应该各触发一次动画（各覆盖那一次等待）。"""
    renderer = _SpyRenderer(Console(file=io.StringIO(), force_terminal=True, width=80))
    events = [
        TaskStarted(task="多轮任务"),
        Thinking(iteration=1),
        AssistantReplyDelta(text="第一轮输出"),
        Thinking(iteration=2),
        ToolCallDelta(index=0, call_id="call_1", name="write_file", arguments_delta="{}"),
        TaskFinished(reason=StopReason.COMPLETED),
    ]
    app = _FakeExecuteApp(renderer, events)

    App._execute(app, "多轮任务")

    assert renderer.spinner_calls == 2


def test_execute_does_not_wrap_sends_before_any_thinking():
    """还没见过 Thinking 之前（比如取到 TaskStarted 这一次 send），不应该有动画。"""
    renderer = _SpyRenderer(Console(file=io.StringIO(), force_terminal=True, width=80))
    events = [TaskFinished(reason=StopReason.COMPLETED)]
    app = _FakeExecuteApp(renderer, events)

    App._execute(app, "空任务")

    assert renderer.spinner_calls == 0


def test_execute_keyboard_interrupt_inside_spinner_still_aborts():
    """转圈动画包住的那次 send() 里抛 KeyboardInterrupt，中断路径要照常触发。"""

    class _InterruptingRuntime:
        tool_limits = ToolLimits()

        def run(self, task: str, *, file_refs=None):
            yield Thinking(iteration=1)
            raise KeyboardInterrupt()

    renderer = _SpyRenderer(Console(file=io.StringIO(), force_terminal=True, width=80))
    app = _FakeExecuteApp(renderer, [])
    app.runtime = _InterruptingRuntime()

    reason = App._execute(app, "会被中断的任务")

    assert reason == StopReason.USER_ABORT
    assert renderer.spinner_calls == 1


# ---------------------------------------------------------------- /skill 手动提醒


def test_execute_prepends_pending_skill_reminder_once():
    """``/skill <name>`` 设的提醒要拼进下一条任务的文本，且只消费这一次。"""
    renderer = _SpyRenderer(Console(file=io.StringIO(), force_terminal=True, width=80))
    events = [TaskFinished(reason=StopReason.COMPLETED)]
    app = _FakeExecuteApp(renderer, events)
    app._pending_skill_reminder = "[手动指定的 Skill · demo]\n触发条件：x\n\n正文"

    App._execute(app, "第一条任务")
    App._execute(app, "第二条任务")

    assert app.runtime.received_tasks[0] == "[手动指定的 Skill · demo]\n触发条件：x\n\n正文\n\n第一条任务"
    assert app.runtime.received_tasks[1] == "第二条任务"
    assert app._pending_skill_reminder is None


def test_execute_without_pending_reminder_sends_task_unchanged():
    renderer = _SpyRenderer(Console(file=io.StringIO(), force_terminal=True, width=80))
    events = [TaskFinished(reason=StopReason.COMPLETED)]
    app = _FakeExecuteApp(renderer, events)

    App._execute(app, "普通任务")

    assert app.runtime.received_tasks == ["普通任务"]


# ---------------------------------------------------------------- _instruction_labels


class _FakePromptStack:
    """够 ``App._instruction_labels`` 用 duck typing 调用的最小替身。"""

    def __init__(self, extra: list[PromptLayer]) -> None:
        self.extra = extra

    def refresh_files(self) -> None:
        pass


class _FakeInstructionLabelsApp:
    def __init__(self, extra: list[PromptLayer]) -> None:
        self.runtime = SimpleNamespace(prompt_stack=_FakePromptStack(extra))


def test_instruction_labels_summarizes_skill_count_instead_of_listing():
    """Skill 层不逐条列标题，汇总成一句"启用了几个"，避免横幅那一行被塞满。"""
    extra = [
        PromptLayer(kind=PromptLayerKind.USER_INSTRUCTIONS, title="用户指令", text="x"),
        PromptLayer(kind=PromptLayerKind.SKILL, title="Skill · a（个人）", text="x"),
        PromptLayer(kind=PromptLayerKind.SKILL, title="Skill · b（项目）", text="x"),
    ]
    app = _FakeInstructionLabelsApp(extra)

    labels = App._instruction_labels(app)

    assert labels == ["用户指令", "Skill 已启用 2 个"]


def test_instruction_labels_omits_skill_summary_when_none_enabled():
    extra = [PromptLayer(kind=PromptLayerKind.USER_INSTRUCTIONS, title="用户指令", text="x")]
    app = _FakeInstructionLabelsApp(extra)

    labels = App._instruction_labels(app)

    assert labels == ["用户指令"]


def test_instruction_labels_only_skill_summary_when_no_other_layers():
    extra = [PromptLayer(kind=PromptLayerKind.SKILL, title="Skill · a（个人）", text="x")]
    app = _FakeInstructionLabelsApp(extra)

    labels = App._instruction_labels(app)

    assert labels == ["Skill 已启用 1 个"]
