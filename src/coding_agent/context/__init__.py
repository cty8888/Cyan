"""上下文层：装配 Message / ToolHistory 为模型 wire 格式。"""

from .builder import ContextBuilder
from .types import ContextPolicy

__all__ = ["ContextBuilder", "ContextPolicy"]
