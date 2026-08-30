"""Prompt Layer：identity 与 cyan.md 等指令在组窗时叠进 system 角色。"""

from .files import INSTRUCTION_FILENAME, load_instruction_layers, project_instruction_path
from .stack import PromptStack
from .types import PromptLayer, PromptLayerKind

__all__ = [
    "INSTRUCTION_FILENAME",
    "PromptLayer",
    "PromptLayerKind",
    "PromptStack",
    "load_instruction_layers",
    "project_instruction_path",
]
