"""todo_write —— 模型自己维护的任务规划清单（对齐 Claude Code 的 TodoWrite）。

每次调用都传**完整**清单（覆盖式更新，不是增量 patch）；执行结果里把清单渲染成
勾选框文本回喂模型，方便它在下一轮确认状态。清单本身存在 ``Session.todos`` 上，
跟随 checkpoint / sidecar meta 持久化，`/todos` 命令可查看。
"""

from __future__ import annotations

from typing import Any

from ...errors import ToolError
from ...session import TodoItem, TodoStatus
from ..base import Tool
from ..types import ToolCapability, ToolContext, ToolRunResult

TODO_WRITE_NAME = "todo_write"
TODO_WRITE_DESCRIPTION = (
    "创建或更新当前任务的结构化清单，用于规划多步骤工作与跟踪进度。"
    "每次调用都要传入完整清单（覆盖上一次的内容，不是增量修改）。"
    "适合 3 步以上、涉及多个文件或需要用户看到进度的任务；"
    "单步就能完成的琐碎任务不需要用它。"
    "同一时刻最多一项 in_progress——开始做一项前先把它标成 in_progress，"
    "做完立刻标成 completed 再开始下一项，不要攒到最后一次性打勾。"
)
TODO_WRITE_PARAMETERS = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "完整任务清单，按执行顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "任务描述（祈使句），如「修复登录 bug」"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                    "activeForm": {
                        "type": "string",
                        "description": "进行中态的现在进行时描述，如「正在修复登录 bug」",
                    },
                },
                "required": ["content", "status", "activeForm"],
            },
        },
    },
    "required": ["todos"],
}

_STATUS_MARK = {
    TodoStatus.COMPLETED: "x",
    TodoStatus.IN_PROGRESS: "~",
    TodoStatus.PENDING: " ",
}


class TodoWriteTool(Tool):
    name = TODO_WRITE_NAME
    description = TODO_WRITE_DESCRIPTION
    capability = ToolCapability.WRITE
    parameters = TODO_WRITE_PARAMETERS

    def run(self, ctx: ToolContext, todos: Any = None, **kwargs: Any) -> ToolRunResult:
        items = _parse_todos(todos)
        ctx.todos.set(items)
        if not items:
            return ToolRunResult.success("任务清单已清空。")
        return ToolRunResult.success(render_checklist(items), todos=[item.to_json() for item in items])


def _parse_todos(raw: Any) -> list[TodoItem]:
    """``base.validate_args`` 只查到「是不是 list」，元素结构在这里手动校验。"""
    if raw is None:
        raise ToolError("缺少必填参数：todos")
    if not isinstance(raw, list):
        raise ToolError("todos 必须是数组")

    items: list[TodoItem] = []
    in_progress_count = 0
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ToolError(f"todos[{index}] 必须是对象")
        content = str(entry.get("content") or "").strip()
        if not content:
            raise ToolError(f"todos[{index}].content 不能为空")
        raw_status = str(entry.get("status") or "").strip()
        try:
            status = TodoStatus(raw_status)
        except ValueError:
            raise ToolError(
                f"todos[{index}].status 必须是 pending / in_progress / completed 之一，收到 {raw_status!r}"
            ) from None
        active_form = str(entry.get("activeForm") or entry.get("active_form") or "").strip()
        if status is TodoStatus.IN_PROGRESS:
            in_progress_count += 1
        items.append(TodoItem(content=content, status=status, active_form=active_form))

    if in_progress_count > 1:
        raise ToolError("同一时刻最多只能有一项 in_progress，请先完成或搁置其它进行中的任务")
    return items


def render_checklist(items: list[TodoItem]) -> str:
    """把清单渲染成勾选框文本，供回喂模型与 /todos 命令共用。"""
    lines = [f"[{_STATUS_MARK[item.status]}] {item.content}" for item in items]
    return "\n".join(lines)
