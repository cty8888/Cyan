"""工具统一契约。

新增一个工具 = 继承 ``Tool`` + 填四个类属性 + 实现 ``run``，
JSON Schema 由 ``to_schema()`` 自动导出给模型的 tool calling 接口。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..errors import InvalidToolArgumentsError

if TYPE_CHECKING:
    from ..config import Config
    from ..core.session import Session
    from ..security.policy import SecurityPolicy


class RiskLevel(Enum):
    """决定该工具是否需要用户审批。"""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"


@dataclass
class ToolResult:
    """工具执行结果。

    ``content`` 是回喂给模型的纯文本，``metadata`` 只给 CLI 渲染用（如 diff），不进上下文。
    """

    ok: bool
    content: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **metadata: Any) -> ToolResult:
        return cls(ok=True, content=content, metadata=metadata)

    @classmethod
    def failure(cls, error: str, **metadata: Any) -> ToolResult:
        return cls(ok=False, error=error, metadata=metadata)

    def to_model_text(self) -> str:
        if self.ok:
            return self.content or "(执行成功，无输出)"
        return f"错误：{self.error}"


@dataclass
class ToolContext:
    """工具执行时能拿到的运行环境。"""

    workspace: Path
    policy: SecurityPolicy
    config: Config
    session: Session


class Tool(ABC):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    risk: ClassVar[RiskLevel] = RiskLevel.READ
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

    def describe(self, args: dict[str, Any], policy: SecurityPolicy) -> tuple[str, str | None, str]:
        """生成审批面板要展示的 (摘要, 细节, 细节格式)。子类按需覆盖。"""
        return f"{self.name}({_compact_json(args)})", None, "text"

    @abstractmethod
    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        """执行工具。可预期的失败请抛 ``ToolError`` 子类或返回 ``ToolResult.failure``。"""


# --------------------------------------------------------------------------
# 轻量 JSON Schema 校验：只覆盖工具定义会用到的子集，避免引入额外依赖
# --------------------------------------------------------------------------

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_args(schema: dict[str, Any], args: dict[str, Any], tool_name: str = "") -> dict[str, Any]:
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
    expected = spec.get("type")
    if expected in _TYPE_MAP:
        # bool 是 int 的子类，这里要单独排除，避免 true 被当成整数通过
        if expected in {"integer", "number"} and isinstance(value, bool):
            raise InvalidToolArgumentsError(f"工具 {tool_name} 的参数 {key} 应为 {expected}，收到 boolean")
        if not isinstance(value, _TYPE_MAP[expected]):
            # 模型常把数字写成字符串，这里做一次宽松转换而不是直接报错
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
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if expected == "integer":
            return int(text)
        if expected == "number":
            return float(text)
        if expected == "boolean":
            lowered = text.lower()
            if lowered in {"true", "false"}:
                return lowered == "true"
    except ValueError:
        return None
    return None


def _compact_json(args: dict[str, Any], limit: int = 120) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."
