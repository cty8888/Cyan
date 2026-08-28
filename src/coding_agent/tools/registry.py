"""工具注册表 —— schema 导出与调用分发。

可预期异常在此转换为 ``ToolResult``，避免中断 Agent Loop。
"""

from __future__ import annotations

import traceback
from typing import Any, Iterator

from ..errors import AgentError, ToolError, ToolNotFoundError
from .base import Tool, ToolContext, ToolResult


class ToolRegistry:
    """持有已注册工具，提供 schema 导出与统一执行入口。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
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
        return [tool.to_schema() for tool in self._tools.values()]

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具；任何失败都以 ``ToolResult`` 返回。"""
        try:
            tool = self.get(name)
            normalized = tool.validate(args)
            return tool.run(ctx, **normalized)
        except ToolError as exc:
            return ToolResult.failure(str(exc))
        except FileNotFoundError as exc:
            return ToolResult.failure(f"文件或目录不存在：{exc}")
        except PermissionError as exc:
            return ToolResult.failure(f"权限不足：{exc}")
        except IsADirectoryError as exc:
            return ToolResult.failure(f"目标是一个目录，无法按文件处理：{exc}")
        except UnicodeDecodeError:
            return ToolResult.failure("文件不是 UTF-8 文本，无法读取（可能是二进制文件）")
        except OSError as exc:
            return ToolResult.failure(f"系统调用失败：{exc}")
        except AgentError as exc:
            return ToolResult.failure(str(exc))
        except Exception as exc:  # noqa: BLE001 — 工具内部 bug 也不应中断循环
            return ToolResult.failure(
                f"工具 {name} 执行时发生内部错误：{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(limit=5),
            )

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


def build_default_registry() -> ToolRegistry:
    """注册默认工具集。新增工具在此追加一行即可。"""
    from .bash import BashTool
    from .edit_file import EditFileTool
    from .list_dir import ListDirTool
    from .read_file import ReadFileTool
    from .write_file import WriteFileTool

    registry = ToolRegistry()
    registry.register(ListDirTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(BashTool())
    return registry
