"""工具契约 —— Tool 基类与参数校验。

新增工具：继承 ``Tool``，填写 name / description / capability / risk / parameters，实现 ``run``；
JSON Schema 由 ``to_schema()`` 自动导出。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..errors import InvalidToolArgumentsError
from .types import RiskLevel, ToolCapability, ToolContext, ToolRunResult

COMPACT_JSON_LIMIT = 120
JSON_SCHEMA_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}

if TYPE_CHECKING:
    from ..session import WorkspaceAccess


class Tool(ABC):
    """单个工具的抽象基类。"""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    capability: ClassVar[ToolCapability] = ToolCapability.READ
    risk: ClassVar[RiskLevel] = RiskLevel.MINIMAL
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def to_schema(self) -> dict[str, Any]:
        """导出为 OpenAI 兼容的 function calling 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        """校验并填充默认值，返回规范化后的参数。"""
        return validate_args(self.parameters, args, self.name)

    def describe(
        self,
        args: dict[str, Any],
        workspace: Path,
        workspace_access: WorkspaceAccess | None = None,
    ) -> tuple[str, str | None, str]:
        """生成审批面板内容：（摘要、细节、细节格式）。子类按需覆盖。

        ``workspace_access`` 可选：传入时，预览会走与 ``run()`` 相同的前置检查
        （例如「修改前必须先读过」），避免审批面板展示一份执行时根本不会发生的 diff。
        """
        return f"{self.name}({_compact_json(args)})", None, "text"

    @abstractmethod
    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolRunResult:
        """执行工具。可预期失败应抛 ``ToolError`` 子类或返回 ``ToolRunResult.failure``。"""


def validate_args(schema: dict[str, Any], args: dict[str, Any], tool_name: str = "") -> dict[str, Any]:
    """按 JSON Schema 校验参数，填充默认值并返回规范化结果。"""
    if not isinstance(args, dict):
        raise InvalidToolArgumentsError(f"工具 {tool_name} 的参数必须是 JSON 对象")

    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    missing = [key for key in required if args.get(key) is None]
    if missing:
        raise InvalidToolArgumentsError(
            f"工具 {tool_name} 缺少必填参数：{', '.join(missing)}。必填参数为 {', '.join(required)}"
        )

    unknown = [key for key in args if key not in properties]
    if unknown:
        raise InvalidToolArgumentsError(
            f"工具 {tool_name} 不支持参数 {', '.join(unknown)}，可用参数为 {', '.join(properties) or '无'}"
        )

    normalized: dict[str, Any] = {}
    for key, spec in properties.items():
        if key in args and args[key] is not None:
            normalized[key] = _check_type(key, args[key], spec, tool_name)
        elif "default" in spec:
            normalized[key] = spec["default"]
    return normalized


def _check_type(key: str, value: Any, spec: dict[str, Any], tool_name: str) -> Any:
    """校验单个参数的类型与 enum 约束。"""
    expected = spec.get("type")
    if expected in JSON_SCHEMA_TYPE_MAP:
        # bool 是 int 子类，需单独排除
        if expected in {"integer", "number"} and isinstance(value, bool):
            raise InvalidToolArgumentsError(f"工具 {tool_name} 的参数 {key} 应为 {expected}，收到 boolean")
        if expected == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, JSON_SCHEMA_TYPE_MAP[expected]):
            # 模型常把数字写成字符串，尝试宽松转换
            coerced = _coerce_scalar(value, expected)
            if coerced is None:
                raise InvalidToolArgumentsError(
                    f"工具 {tool_name} 的参数 {key} 类型应为 {expected}，收到 {type(value).__name__}"
                )
            value = coerced

    allowed = spec.get("enum")
    if allowed and value not in allowed:
        raise InvalidToolArgumentsError(
            f"工具 {tool_name} 的参数 {key} 取值必须是 {allowed} 之一，收到 {value!r}"
        )
    return value


def _coerce_scalar(value: Any, expected: str) -> Any:
    """尝试把字符串形式的标量转为目标类型。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if expected == "integer":
            number = float(text) if "." in text or "e" in text.lower() else int(text)
            if isinstance(number, float):
                if not number.is_integer():
                    return None
                return int(number)
            return number
        if expected == "number":
            return float(text)
        if expected == "boolean":
            lowered = text.lower()
            if lowered in {"true", "false"}:
                return lowered == "true"
    except ValueError:
        return None
    return None


def _compact_json(args: dict[str, Any], limit: int = COMPACT_JSON_LIMIT) -> str:
    """序列化参数摘要，过长时截断。"""
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."
