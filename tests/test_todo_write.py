"""todo_write 工具：整体覆盖式更新、参数校验、与 Session.todos 的联动。"""

from __future__ import annotations

from cyan.errors import ToolError
from cyan.session import TodoItem, TodoStatus
from cyan.tools.builtin.todo_write import render_checklist


def test_run_replaces_session_todos(env):
    args = {
        "todos": [
            {"content": "分析代码结构", "status": "completed", "activeForm": "正在分析代码结构"},
            {"content": "实现功能", "status": "in_progress", "activeForm": "正在实现功能"},
            {"content": "编写测试", "status": "pending", "activeForm": "正在编写测试"},
        ]
    }

    result = env.registry.execute("todo_write", args, env.ctx)

    assert result.ok
    assert [item.content for item in env.session.todos] == ["分析代码结构", "实现功能", "编写测试"]
    assert env.session.todos[0].status is TodoStatus.COMPLETED
    assert env.session.todos[1].status is TodoStatus.IN_PROGRESS
    assert env.session.todos[1].active_form == "正在实现功能"


def test_run_overwrites_previous_list_not_merges(env):
    env.registry.execute(
        "todo_write",
        {"todos": [{"content": "旧任务", "status": "pending", "activeForm": "正在做旧任务"}]},
        env.ctx,
    )
    env.registry.execute(
        "todo_write",
        {"todos": [{"content": "新任务", "status": "pending", "activeForm": "正在做新任务"}]},
        env.ctx,
    )

    assert [item.content for item in env.session.todos] == ["新任务"]


def test_run_with_empty_list_clears_todos(env):
    env.registry.execute(
        "todo_write",
        {"todos": [{"content": "任务", "status": "pending", "activeForm": "正在做任务"}]},
        env.ctx,
    )
    result = env.registry.execute("todo_write", {"todos": []}, env.ctx)

    assert result.ok
    assert env.session.todos == []
    assert "已清空" in result.content


def test_result_content_renders_checklist_marks(env):
    result = env.registry.execute(
        "todo_write",
        {
            "todos": [
                {"content": "已完成", "status": "completed", "activeForm": "x"},
                {"content": "进行中", "status": "in_progress", "activeForm": "x"},
                {"content": "待办", "status": "pending", "activeForm": "x"},
            ]
        },
        env.ctx,
    )
    assert result.content == "[x] 已完成\n[~] 进行中\n[ ] 待办"


def test_result_metadata_carries_todos_json(env):
    result = env.registry.execute(
        "todo_write",
        {"todos": [{"content": "任务", "status": "pending", "activeForm": "正在做任务"}]},
        env.ctx,
    )
    assert result.metadata["todos"] == [
        {"content": "任务", "status": "pending", "active_form": "正在做任务"}
    ]


def test_missing_todos_field_fails_validation(env):
    result = env.registry.execute("todo_write", {}, env.ctx)
    assert not result.ok
    assert "todos" in (result.error or "")


def test_empty_content_is_rejected(env):
    result = env.registry.execute(
        "todo_write",
        {"todos": [{"content": "  ", "status": "pending", "activeForm": "x"}]},
        env.ctx,
    )
    assert not result.ok
    assert "content" in (result.error or "")


def test_invalid_status_is_rejected(env):
    result = env.registry.execute(
        "todo_write",
        {"todos": [{"content": "任务", "status": "done", "activeForm": "x"}]},
        env.ctx,
    )
    assert not result.ok
    assert "status" in (result.error or "")


def test_more_than_one_in_progress_is_rejected(env):
    result = env.registry.execute(
        "todo_write",
        {
            "todos": [
                {"content": "任务1", "status": "in_progress", "activeForm": "x"},
                {"content": "任务2", "status": "in_progress", "activeForm": "x"},
            ]
        },
        env.ctx,
    )
    assert not result.ok
    assert "in_progress" in (result.error or "")
    # 校验失败不应该改动已有清单
    assert env.session.todos == []


def test_non_list_todos_is_rejected(env):
    """走 base.validate_args 的话，非 list 早在 schema 层被拦下；这里直接跳过 validate
    走 ``_parse_todos`` 自己的校验分支（模型偶尔会传字符串而不是数组）。
    """
    result = env.registry.execute("todo_write", {"todos": "不是数组"}, env.ctx, validated=True)
    assert not result.ok
    assert "数组" in (result.error or "")


def test_render_checklist_helper():
    items = [
        TodoItem(content="a", status=TodoStatus.COMPLETED),
        TodoItem(content="b", status=TodoStatus.IN_PROGRESS),
        TodoItem(content="c", status=TodoStatus.PENDING),
    ]
    assert render_checklist(items) == "[x] a\n[~] b\n[ ] c"


def test_active_form_accepts_snake_case_fallback(env):
    """Schema 用 activeForm（驼峰对齐 Claude Code），兜底也认 active_form。"""
    result = env.registry.execute(
        "todo_write",
        {"todos": [{"content": "任务", "status": "pending", "active_form": "正在做任务"}]},
        env.ctx,
    )
    assert result.ok
    assert env.session.todos[0].active_form == "正在做任务"


def test_todo_write_tool_error_is_toolerror_subclass():
    from cyan.tools.builtin.todo_write import _parse_todos

    try:
        _parse_todos(None)
        assert False, "应该抛出 ToolError"
    except ToolError:
        pass
