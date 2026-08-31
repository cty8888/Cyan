"""Runtime.create()：compact/loop/tools 三个策略域都要跟 AgentSettings 解耦。"""

from __future__ import annotations

from cyan.core.runtime import Runtime
from cyan.settings import LoopLimits, ToolLimits

from .conftest import FakeLLM


def test_loop_limits_is_a_decoupled_copy(env):
    runtime = Runtime.create(env.settings, FakeLLM([]), env.registry, env.permissions, env.session)

    assert runtime.loop_limits == env.settings.loop
    assert runtime.loop_limits is not env.settings.loop

    runtime.loop_limits.max_iterations = 999
    assert env.settings.loop.max_iterations != 999


def test_tool_limits_is_a_decoupled_copy(env):
    runtime = Runtime.create(env.settings, FakeLLM([]), env.registry, env.permissions, env.session)

    assert runtime.tool_limits == env.settings.tools
    assert runtime.tool_limits is not env.settings.tools

    runtime.tool_limits.max_file_read_chars = 1
    assert env.settings.tools.max_file_read_chars != 1


def test_create_uses_settings_as_initial_values(make_env):
    env = make_env(
        loop=LoopLimits(max_iterations=7),
        tools=ToolLimits(max_dir_entries=42),
    )
    runtime = Runtime.create(env.settings, FakeLLM([]), env.registry, env.permissions, env.session)

    assert runtime.loop_limits.max_iterations == 7
    assert runtime.tool_limits.max_dir_entries == 42
