"""工具注册表：schema 导出、名称分发与统一执行。"""

from __future__ import annotations

import traceback
from typing import Any, Iterator

from ..errors import AgentError, ToolError, ToolNotFoundError
from .base import Tool
from .types import ToolContext, ToolRunResult


class ToolRegistry:
    """持有已注册工具，提供 schema 导出与统一执行入口。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        """按 ``tool.name`` 注册；重名或未定义 name 会立刻失败。"""
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 未定义 name")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(sorted(self._tools)) or "无"
            raise ToolNotFoundError(f"不存在名为 {name} 的工具，可用工具：{available}")
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def schemas(self) -> list[dict[str, Any]]:
        """导出全部工具的 OpenAI function calling 定义。"""
        return [tool.to_schema() for tool in self._tools.values()]

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        *,
        validated: bool = False,
    ) -> ToolRunResult:
        """执行工具；任何失败都以 ``ToolRunResult`` 返回。

        ``validated=True`` 表示调用方已做过 ``tool.validate()``（Agent Loop 在审批前会先规范化参数），
        此处不再重复校验。
        """
        try:
            tool = self.get(name)
            normalized = args if validated else tool.validate(args)
            return tool.run(ctx, **normalized)
        except ToolError as exc:
            return ToolRunResult.failure(str(exc))
        except FileNotFoundError as exc:
            return ToolRunResult.failure(f"文件或目录不存在：{exc}")
        except PermissionError as exc:
            return ToolRunResult.failure(f"权限不足：{exc}")
        except IsADirectoryError as exc:
            return ToolRunResult.failure(f"目标是一个目录，无法按文件处理：{exc}")
        except UnicodeDecodeError:
            return ToolRunResult.failure("文件不是 UTF-8 文本，无法读取（可能是二进制文件）")
        except OSError as exc:
            return ToolRunResult.failure(f"系统调用失败：{exc}")
        except AgentError as exc:
            return ToolRunResult.failure(str(exc))
        except Exception as exc:  # noqa: BLE001 — 工具内部 bug 也不应中断循环
            return ToolRunResult.failure(
                f"工具 {name} 执行时发生内部错误：{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(limit=5),
            )

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


def build_default_registry() -> ToolRegistry:
    """注册默认工具集。新增工具在此追加一行即可。"""
    from .builtin.bash import BashTool
    from .builtin.edit_file import EditFileTool
    from .builtin.list_dir import ListDirTool
    from .builtin.read_file import ReadFileTool
    from .builtin.write_file import WriteFileTool

    registry = ToolRegistry()
    registry.register(ListDirTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(BashTool())
    return registry
