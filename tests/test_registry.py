"""工具注册表：未知工具、参数校验。"""

from __future__ import annotations


def test_unknown_tool(env):
    result = env.registry.execute("no_such_tool", {}, env.ctx)
    assert not result.ok
    assert "不存在名为" in (result.error or "")


def test_default_registry_has_seven_tools(env):
    names = {tool.name for tool in env.registry}
    assert names == {"list_dir", "read_file", "write_file", "edit_file", "bash", "glob", "grep"}
