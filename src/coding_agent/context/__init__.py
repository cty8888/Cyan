"""上下文层：装配 Message / ToolHistory 为模型 wire 格式。"""

from .builder import ContextBuilder
from .config import ContextConfig

__all__ = ["ContextBuilder", "ContextConfig"]
